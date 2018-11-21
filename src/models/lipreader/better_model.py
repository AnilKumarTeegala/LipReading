from allennlp.nn.util import masked_softmax, sort_batch_by_length
import torch
import torch.nn as nn
import torch.nn.functional as F

supported_rnns = {
    'lstm': nn.LSTM,
    'rnn': nn.RNN,
    'gru': nn.GRU
}
supported_rnns_inv = dict((v, k) for k, v in supported_rnns.items())

class VideoEncoder(nn.Module):
    def __init__(self, frame_dim, hidden_size,
                 rnn_type=nn.LSTM, num_layers=1, bidirectional=True, rnn_dropout=0):
        super(VideoEncoder).__init__()

        self.hidden_size = hidden_size
        self.rnn_type = rnn_type
        self.num_layers = num_layers
        self.bidirectional = bidirectional
        self.rnn_dropout = rnn_dropout

        self.rnn = self.rnn_type(self.frame_dim, self.hidden_size,
                                 num_layers=self.num_layers, bidirectional=self.bidirectional,
                                 batch_first=True, dropout=self.rnn_dropout)

    def forward(self,
                frames: torch.FloatTensor,
                frame_lens: torch.LongTensor):
        """
        frames: (batch_size, seq_len, frame_dim)
        frame_lens: (batch_size, )
        """
        (sorted_frames, sorted_frame_lens,
            restoration_indices, _) = sort_batch_by_length(frames, frame_lens)

        packed_frames = nn.utils.rnn.pack_padded_sequence(sorted_frames,
                            sorted_frame_lens.data.cpu().numpy() if sorted_frame_lens.is_cuda else sorted_frame_lens.data.numpy(),
                            batch_first=True)

        # final_state: (num_layers * num_dir, batch_size, hidden_size) (*2 if LSTM)
        packed_hidden_states, final_state = self.rnn(packed_frames)

        # (batch_size, seq_len, num_dir * hidden_size)
        hidden_states, _ = nn.utils.rnn.pad_packed_sequence(packed_hidden_states, batch_first=True)

        # (num_layers, batch_size, hidden_size * num_dir) (*2 if LSTM)
        if self.bidirectional:
            final_state = self._cat_directions(final_state)

        hidden_states = hidden_states.index_select(0, restoration_indices)
        if isinstance(final_state, tuple):  # LSTM
            final_state = (final_state[0].index_select(1, restoration_indices),
                           final_state[1].index_select(1, restoration_indices))
        else:
            final_state = final_state.index_select(1, restoration_indices)

        return hidden_states, final_state

    def _cat_directions(self, final_state):
        """
        final_state must come from a bidirectional RNN
        (num_layers * num_dir, batch_size, hidden_size) -->
        (num_layers, batch_size, hidden_size * num_dir)
        """
        def _cat(s):
            return torch.cat([s[0:s.shape[0]:2], s[1:s.shape[0]:2]], dim=2)

        if isinstance(final_state, tuple):  # LSTM
            final_state = tuple(_cat(s) for s in final_state)
        else:
            final_state = _cat(final_state)

        return final_state

class CharDecoder(nn.Module):
    def __init__(self, encoder: VideoEncoder, char_dim, output_size, char_padding_idx, rnn_dropout=0):
        super(CharDecoder).__init__()

        self.hidden_size = encoder.hidden_size * (2 if encoder.bidirectional else 1)
        self.rnn_type = encoder.rnn_type
        self.num_layers = encoder.num_layers
        self.rnn_dropout = rnn_dropout
        self.char_dim = char_dim
        self.output_size = output_size
        self.char_padding_idx = char_padding_idx

        self.embedding = nn.Embedding(self.output_size, self.char_dim, padding_idx=self.char_padding_idx)
        self.rnn = self.rnn_type(self.char_dim, self.hidden_size,
                                 num_layers=self.num_layers, batch_first=True, dropout=self.rnn_dropout)
        self.attn_proj = nn.Linear(2 * self.hidden_size, 1)
        self.concat_layer = nn.Linear(2 * self.hidden_size, self.hidden_size)
        self.output_proj = nn.Linear(self.hidden_size, self.output_size)

    def forward(self,
                char: torch.LongTensor,
                previous_state: torch.FloatTensor,
                encoder_lens: torch.LongTensor,
                encoder_hidden_states: torch.FloatTensor):
        """
        char: (batch_size, )
        previous_state: (num_layers, batch_size, hidden_size)
        encoder_lens: (batch_size, )
        encoder_hidden_states: (batch_size, en_seq_len, hidden_size)
        """
        batch_size = char.shape[0]
        en_seq_len = encoder_hidden_states.shape[1]

        # (batch_size, en_seq_len)
        encoder_mask = torch.arange(en_seq_len).expand(batch_size, en_seq_len) < encoder_lens.unsqueeze(dim=1)

        # (batch_size, char_dim)
        embedded_char = self.embedding(char)
        # (batch_size, seq_len=1, char_dim)
        char = char.unsqueeze(dim=1)

        # hidden_state: (batch_size, seq_len=1, hidden_size)
        # final_state: (num_layers, batch_size, hidden_size)
        hidden_state, final_state = self.rnn(char, previous_state)

        # (batch_size, en_seq_len, hidden_size)
        expanded_hidden_state = hidden_state.expand_as(encoder_hidden_states)
        # (batch_size, en_seq_len)
        attn_logits = self.attn_proj(torch.cat([encoder_hidden_states, expanded_hidden_state], dim=2)).squeeze(dim=-1)
        # (batch_size, 1, en_seq_len)
        attn_weights = masked_softmax(attn_logits, encoder_mask, dim=-1).unsqueeze(dim=1)
        # (batch_size, hidden_size)
        context = attn_weights.bmm(encoder_hidden_states).unsqueeze(dim=1)

        # (batch_size, hidden_size)
        new_hidden_state = self.concat_layer(torch.cat([context, hidden_state.squeeze(dim=1)], dim=1))
        new_hidden_state = F.tanh(new_hidden_state)

        # (batch_size, output_size)
        output_logits = self.output_proj(new_hidden_state)

        return output_logits, final_state