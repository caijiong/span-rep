import torch
import torch.nn as nn
from encoders.pretrained_transformers.span_reprs import get_span_module


class TaskModel(nn.Module):
    def __init__(self, encoder,
                 span_dim=256, pool_method='avg', just_last_layer=False,
                 **kwargs):
        super(TaskModel, self).__init__()
        self.encoder = encoder
        self.just_last_layer = just_last_layer
        self.pool_method = pool_method
        self.span_net = nn.ModuleDict()
        self.span_net['0'] = get_span_module(
            method=pool_method, input_dim=self.encoder.hidden_size,
            use_proj=True, proj_dim=span_dim)
        self.pooled_dim = self.span_net['0'].get_output_dim()

        self.label_net = nn.Sequential(
            nn.Linear(self.pooled_dim, span_dim),
            nn.Tanh(),
            nn.LayerNorm(span_dim),
            nn.Dropout(0.2),
            nn.Linear(span_dim, 1),
            nn.Sigmoid()
        )

        self.training_criterion = nn.BCELoss()

    def get_other_params(self):
        core_encoder_param_names = set()
        for name, param in self.encoder.model.named_parameters():
            if param.requires_grad:
                core_encoder_param_names.add(name)

        other_params = []
        print("\nParams outside core transformer params:\n")
        for name, param in self.named_parameters():
            if param.requires_grad and name not in core_encoder_param_names:
                print(name, param.data.size())
                other_params.append(param)
        print("\n")
        return other_params

    def get_core_params(self):
        return self.encoder.model.parameters()

    def calc_span_repr(self, encoded_input, span_indices, index='0'):
        span_start, span_end = span_indices[:, 0], span_indices[:, 1]
        span_repr = self.span_net[index](encoded_input, span_start, span_end)
        return span_repr

    def forward(self, batch_data):
        text, text_len = batch_data.text
        encoded_input = self.encoder(text.cuda(), just_last_layer=self.just_last_layer)

        s_repr = self.calc_span_repr(encoded_input, batch_data.span.cuda())
        pred_label = self.label_net(s_repr)
        pred_label = torch.squeeze(pred_label, dim=-1)
        label = batch_data.label.cuda().float()
        loss = self.training_criterion(pred_label, label)
        if self.training:
            return loss
        else:
            return loss, pred_label, label
