import itertools
from pathlib import Path
from logging import getLogger
from typing import List, Optional, Dict, Tuple, Union, Any

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch import Tensor
# from apex import amp

from deeppavlov.core.commands.utils import expand_path
from transformers import AutoConfig, AutoTokenizer, AutoModel, BertModel, BertTokenizer
from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.common.registry import register
from deeppavlov.core.models.torch_model import TorchModel

log = getLogger(__name__)


@register('torch_transformers_ranker')
class BertRanker(TorchModel):

    def __init__(
            self,
            model_name: str,
            text_encoder_save_path: str,
            descr_encoder_save_path: str,
            pretrained_bert: str = None,
            bert_config_file: Optional[str] = None,
            criterion: str = "CrossEntropyLoss",
            optimizer: str = "AdamW",
            optimizer_parameters: Dict = {"lr": 5e-5, "weight_decay": 0.01, "eps": 1e-6},
            return_probas: bool = False,
            attention_probs_keep_prob: Optional[float] = None,
            hidden_keep_prob: Optional[float] = None,
            clip_norm: Optional[float] = None,
            threshold: Optional[float] = None,
            **kwargs
    ):
        self.text_encoder_save_path = text_encoder_save_path
        self.descr_encoder_save_path = descr_encoder_save_path
        self.pretrained_bert = pretrained_bert
        self.bert_config_file = bert_config_file
        self.return_probas = return_probas
        self.attention_probs_keep_prob = attention_probs_keep_prob
        self.hidden_keep_prob = hidden_keep_prob
        self.clip_norm = clip_norm

        super().__init__(
            model_name=model_name,
            optimizer=optimizer,
            criterion=criterion,
            optimizer_parameters=optimizer_parameters,
            return_probas=return_probas,
            **kwargs)

    def train_on_batch(self, q_features: List[Dict],
                             c_features_list: List[List[Dict]],
                             positive_idx: List[List[int]]) -> float:

        _input = {'positive_idx': positive_idx}
        for elem in ['input_ids', 'attention_mask']:
            inp_elem = [f[elem] for f in q_features]
            _input[f"q_{elem}"] = torch.LongTensor(inp_elem).to(self.device)
        for elem in ['input_ids', 'attention_mask']:
            inp_elem = [f[elem] for c_features in c_features_list for f in c_features]
            _input[f"c_{elem}"] = torch.LongTensor(inp_elem).to(self.device)

        self.model.train()
        self.model.zero_grad()
        self.optimizer.zero_grad()      # zero the parameter gradients

        loss, softmax_scores = self.model(**_input)
        loss.backward()
        self.optimizer.step()

        # Clip the norm of the gradients to prevent the "exploding gradients" problem
        if self.clip_norm:
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.clip_norm)

        if self.lr_scheduler is not None:
            self.lr_scheduler.step()

        return loss.item()

    def __call__(self, q_features: List[Dict],
                       c_features_list: List[List[Dict]]) -> Union[List[int], List[np.ndarray]]:

        self.model.eval()

        _input = {}
        for elem in ['input_ids', 'attention_mask']:
            inp_elem = [f[elem] for f in q_features]
            _input[f"q_{elem}"] = torch.LongTensor(inp_elem).to(self.device)
        for elem in ['input_ids', 'attention_mask']:
            inp_elem = [f[elem] for c_features in c_features_list for f in c_features]
            _input[f"c_{elem}"] = torch.LongTensor(inp_elem).to(self.device)

        with torch.no_grad():
            softmax_scores = self.model(**_input)
            pred = torch.argmax(softmax_scores, dim=1).cpu().numpy()
            
        return pred

    def in_batch_ranking_model(self, **kwargs) -> nn.Module:
        return BertRanking(
            pretrained_bert=self.pretrained_bert,
            text_encoder_save_path=self.text_encoder_save_path,
            descr_encoder_save_path=self.descr_encoder_save_path,
            bert_tokenizer_config_file=self.pretrained_bert,
            device=self.device
        )
        
    def save(self, fname: Optional[str] = None, *args, **kwargs) -> None:
        if fname is None:
            fname = self.save_path
        if not fname.parent.is_dir():
            raise ConfigError("Provided save path is incorrect!")
        weights_path = Path(fname).with_suffix(f".pth.tar")
        log.info(f"Saving model to {weights_path}.")
        torch.save({
            "model_state_dict": self.model.cpu().state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "epochs_done": self.epochs_done
        }, weights_path)
        self.model.to(self.device)
        self.model.save()


class BertRanking(nn.Module):

    def __init__(
            self,
            text_encoder_save_path: str,
            descr_encoder_save_path: str,
            pretrained_bert: str = None,
            bert_tokenizer_config_file: str = None,
            bert_config_file: str = None,
            device: str = "gpu"
    ):
        super().__init__()
        self.pretrained_bert = pretrained_bert
        self.text_encoder_save_path = text_encoder_save_path
        self.descr_encoder_save_path = descr_encoder_save_path
        self.bert_config_file = bert_config_file
        self.device = device

        # initialize parameters that would be filled later
        self.q_encoder, self.c_encoder, self.config, self.bert_config = None, None, None, None
        self.load()

        if Path(bert_tokenizer_config_file).is_file():
            vocab_file = str(expand_path(bert_tokenizer_config_file))
            self.tokenizer = BertTokenizer(vocab_file=vocab_file)
        else:
            tokenizer = BertTokenizer.from_pretrained(pretrained_bert)
        self.q_encoder.resize_token_embeddings(len(tokenizer) + 1)
        self.cls_token_id = tokenizer.cls_token_id
        self.sep_token_id = tokenizer.sep_token_id

    def forward(
            self,
            q_input_ids: Tensor,
            q_attention_mask: Tensor,
            c_input_ids: Tensor,
            c_attention_mask: Tensor,
            positive_idx: List[List[int]] = None
    ) -> Union[Tuple[Any, Tensor], Tuple[Tensor]]:

        q_hidden_states, q_cls_emb, _ = self.q_encoder(input_ids=q_input_ids, attention_mask=q_attention_mask)
        c_hidden_states, c_cls_emb, _ = self.c_encoder(input_ids=c_input_ids, attention_mask=c_attention_mask)
        dot_products = torch.matmul(q_cls_emb, torch.transpose(c_cls_emb, 0, 1))
        softmax_scores = F.log_softmax(dot_products, dim=1)
        if positive_idx is not None:
            loss = F.nll_loss(softmax_scores, torch.tensor(positive_idx).to(softmax_scores.device), reduction="mean")
            return loss, softmax_scores
        else:
            return softmax_scores

    def load(self) -> None:
        if self.pretrained_bert:
            log.info(f"From pretrained {self.pretrained_bert}.")
            self.config = AutoConfig.from_pretrained(
                self.pretrained_bert, output_hidden_states=True
            )
            self.q_encoder = BertModel.from_pretrained(self.pretrained_bert, config=self.config)
            self.c_encoder = BertModel.from_pretrained(self.pretrained_bert, config=self.config)

        elif self.bert_config_file and Path(self.bert_config_file).is_file():
            self.config = AutoConfig.from_json_file(str(expand_path(self.bert_config_file)))
            self.q_encoder = BertModel.from_config(config=self.bert_config)
            self.c_encoder = BertModel.from_config(config=self.bert_config)
        else:
            raise ConfigError("No pre-trained BERT model is given.")

        self.q_encoder.to(self.device)
        self.c_encoder.to(self.device)
        
    def save(self) -> None:
        text_encoder_weights_path = expand_path(self.text_encoder_save_path).with_suffix(f".pth.tar")
        log.info(f"Saving text encoder to {text_encoder_weights_path}.")
        torch.save({"model_state_dict": self.q_encoder.cpu().state_dict()}, text_encoder_weights_path)
        descr_encoder_weights_path = expand_path(self.descr_encoder_save_path).with_suffix(f".pth.tar")
        log.info(f"Saving descr encoder to {descr_encoder_weights_path}.")
        torch.save({"model_state_dict": self.c_encoder.cpu().state_dict()}, descr_encoder_weights_path)
        self.q_encoder.to(self.device)
        self.c_encoder.to(self.device)


@register('torch_bert_cls_encoder')
class TorchBertCLSEncoder:
    def __init__(self, pretrained_bert, weights_path, add_special_tokens=None,
                       do_lower_case: bool = False, device: str = "gpu", **kwargs):
        self.device = torch.device("cuda" if torch.cuda.is_available() and device == "gpu" else "cpu")
        self.pretrained_bert = pretrained_bert
        self.config = AutoConfig.from_pretrained(self.pretrained_bert, output_hidden_states=True)
        self.tokenizer = AutoTokenizer.from_pretrained(self.pretrained_bert, do_lower_case=do_lower_case)
        if add_special_tokens is not None:
            special_tokens_dict = {'additional_special_tokens': add_special_tokens}
            num_added_toks = self.tokenizer.add_special_tokens(special_tokens_dict)
        self.text_encoder = AutoModel.from_config(config=self.config)
        #self.text_encoder.resize_token_embeddings(len(self.tokenizer) + 1)
        self.weights_path = expand_path(weights_path)
        print("weights_path", str(self.weights_path))
        checkpoint = torch.load(self.weights_path, map_location=self.device)
        self.text_encoder.load_state_dict(checkpoint["model_state_dict"])
        self.text_encoder.to(self.device)

    def __call__(self, texts_batch: List[str]):
        tokenizer_input = [[text, None] for text in texts_batch]
        encoding = self.tokenizer.batch_encode_plus(
            tokenizer_input, add_special_tokens = True, pad_to_max_length=True,
            return_attention_mask = True)
        input_ids = encoding["input_ids"]
        attention_mask = encoding["attention_mask"]
        input_ids = torch.LongTensor(input_ids).to(self.device)
        attention_mask = torch.LongTensor(attention_mask).to(self.device)

        _, text_cls_emb, _ = self.text_encoder(input_ids=input_ids, attention_mask=attention_mask)
        text_cls_emb = text_cls_emb.detach().cpu().numpy()
        return text_cls_emb
