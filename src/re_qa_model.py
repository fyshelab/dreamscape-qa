"""Implementation of the T5 Models for Response Generation and Question
Generation Used for relation extraction."""

import gc
import math
import os
import random
from dataclasses import dataclass
from typing import Any, Optional

import numpy
import torch
from transformers import Adafactor, T5ForConditionalGeneration, T5Tokenizer


def white_space_fix(text):
    return " ".join(text.split())


@dataclass
class HyperParameters:
    """General Model configuration."""

    model_path: Optional[str] = None
    batch_size: int = 16
    source_max_length: int = 256
    decoder_max_length: int = 32
    config_file: str = "config.ini"
    gpu: bool = False
    learning_rate: float = 0.0005
    max_epochs: int = 16
    mode: str = "train"
    train: Optional[str] = None
    prediction_file: Optional[str] = None
    seed: int = 8
    test: Optional[str] = None
    dev: Optional[str] = None
    answer_checkpoint: Optional[str] = "_3_model"
    question_checkpoint: Optional[str] = "_3_model"
    checkpoint: Optional[str] = "_3_model"
    training_steps: Optional[int] = 1
    predict_type: Optional[str] = "entity"

    # Related to decoding.
    no_repeat_ngram_size: Optional[int] = 2
    num_search_samples: Optional[int] = 8
    num_neg_samples: Optional[int] = 3
    model_name: str = "MODEL_NAME"


def tuple_of_tensors_to_tensor(tuple_of_tensors):
    return torch.stack(list(tuple_of_tensors), dim=0)


def prepare_response_module_input(
    answer_input_ids=None,
    answer_input_mask=None,
    labels=None,
    target_mask=None,
    num_samples=1,
):
    """Repeat the labels and the target_mask num_samples times in dimension
    1."""
    b_sz, dec_seq_len = labels.size()
    _, src_seq_len = answer_input_ids.size()
    labels = labels.repeat(1, num_samples).view(-1, dec_seq_len)
    target_mask = target_mask.repeat(1, num_samples).view(-1, dec_seq_len)

    return (
        answer_input_ids,
        answer_input_mask,
        target_mask,
        labels,
    )


def set_random_seed(seed: int):
    """Set the random seed, which initializes the random number generator.

    Ensures that runs are reproducible and eliminates differences due to
    randomness.
    """
    random.seed(seed)
    numpy.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def remove_prefix(text, prefix):
    """This function is used to remove prefix key from the text."""
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text  # or whatever


def torch_save(model: torch.nn.Module, path: str):
    """Save the model at the specified path."""
    torch.save(model.state_dict(), path)


def save(model, model_path: str, checkpoint_name: str):
    """Save the model to the specified path name using a checkpoint name."""
    torch_save(model, model_path + "_" + checkpoint_name)


def load_module(model, model_path, checkpoint_name):
    """Load the model from the checkpoint."""
    loaded_weights = torch.load(
        model_path + checkpoint_name,
        map_location=lambda storage, loc: storage,
    )

    # sometimes we the main model wrapped inside a dataparallel or distdataparallel object.
    new_weights = {}
    for key, val in loaded_weights.items():
        new_weights[remove_prefix(key, "module.")] = val
    model.load_state_dict(new_weights)


def clear_cache():
    """Clean unused GPU Cache!"""
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()


def prob_of_sampled_predictions(loss_fct, sample_outputs):
    """Helper function to compute the predictions and the probability of a
    sampled sequence from the output of the generate function in the
    transformers library."""

    # Skip the first pad token generated by the T5 model.
    sampled_predictions = sample_outputs.sequences[:, 1:]
    sampled_scores = sample_outputs.scores
    sampled_scores = tuple_of_tensors_to_tensor(sampled_scores)

    # v: vocab size
    # n: batch_size * num_samples
    # l: sequence len
    l, n, v = sampled_scores.size()
    log_p = -loss_fct(
        sampled_scores.view(-1, v),
        torch.reshape(torch.transpose(sampled_predictions, 0, 1), (l * n,)),
    ).view(l, n)
    pad_mask = torch.transpose(sampled_predictions, 0, 1) == 0
    good_log_p = log_p.masked_fill_(pad_mask, 0.0)
    log_p = torch.sum(good_log_p, dim=0).squeeze()
    return sampled_predictions, log_p


MODEL_NAME = "t5-small"


class REQA(torch.nn.Module):
    """Wrapper class around the T5 Models."""

    def __init__(self, cfg: HyperParameters):
        super(REQA, self).__init__()
        self.config = cfg

        set_random_seed(cfg.seed)

        self.model_path = os.path.join(cfg.model_path, "model")

        # Answer Model tokenizer
        answer_tokenizer = T5Tokenizer.from_pretrained(
            MODEL_NAME#, local_files_only=True
        )

        # Construct the answer model
        answer_model = T5ForConditionalGeneration.from_pretrained(
            MODEL_NAME#, local_files_only=True
        )

        # Question Model tokenizer
        question_tokenizer = T5Tokenizer.from_pretrained(
            MODEL_NAME#, local_files_only=True
        )

        # Construct the question model
        question_model = T5ForConditionalGeneration.from_pretrained(
            MODEL_NAME#, local_files_only=True
        )

        # Pretrained question model tokenizer
        self.init_question_tokenizer = T5Tokenizer.from_pretrained(
            MODEL_NAME#, local_files_only=True
        )

        # Construct the pretrained question model
        self.init_question_model = T5ForConditionalGeneration.from_pretrained(
            MODEL_NAME#, local_files_only=True
        )

        if cfg.mode == "train":
            # Configurations suggested by the T5 paper.
            self.answer_optimizer = Adafactor(
                answer_model.parameters(),
                lr=cfg.learning_rate,
                eps=(1e-30, 1e-3),
                clip_threshold=1.0,
                decay_rate=-0.8,
                beta1=None,
                weight_decay=0.0,
                relative_step=False,
                scale_parameter=False,
                warmup_init=False,
            )

            # Configurations suggested by the T5 paper.
            self.question_optimizer = Adafactor(
                question_model.parameters(),
                lr=cfg.learning_rate,
                eps=(1e-30, 1e-3),
                clip_threshold=1.0,
                decay_rate=-0.8,
                beta1=None,
                weight_decay=0.0,
                relative_step=False,
                scale_parameter=False,
                warmup_init=False,
            )

            if not os.path.exists(cfg.model_path):
                os.makedirs(cfg.model_path)

            load_module(answer_model, self.model_path, cfg.answer_checkpoint)
            load_module(question_model, self.model_path, cfg.question_checkpoint)
            load_module(
                self.init_question_model, self.model_path, cfg.question_checkpoint
            )

        elif cfg.mode in ["test", "inference"]:
            try:
                load_module(answer_model, self.model_path, cfg.answer_checkpoint)
                load_module(question_model, self.model_path, cfg.question_checkpoint)
            except:
                print("could not load the answer and question modules.")

        self.answer_model = answer_model
        self.answer_tokenizer = answer_tokenizer
        self.question_model = question_model
        self.question_tokenizer = question_tokenizer

    def question_beam_predict(
        self, batch, current_device, with_tail_entity=False, num_ret_seqs=1
    ):
        """Use beam search to generate the questions and prepare inputs for the
        answer module."""
        question_input_ids = batch["entity_relation_passage_input_ids"]
        question_input_mask = batch["entity_relation_passage_attention_mask"]
        if self.config.gpu:
            question_input_ids = question_input_ids.to(current_device)
            question_input_mask = question_input_mask.to(current_device)

        if with_tail_entity:
            # the posterior inputs also have the tail entity.
            posterier_question_input_ids = batch["posterier_input_ids"]
            posterier_question_input_mask = batch["posterier_attention_mask"]
            if self.config.gpu:
                posterier_question_input_ids = posterier_question_input_ids.to(
                    current_device
                )
                posterier_question_input_mask = posterier_question_input_mask.to(
                    current_device
                )

        with torch.no_grad():
            if with_tail_entity:
                question_output = self.question_model.generate(
                    input_ids=posterier_question_input_ids,
                    attention_mask=posterier_question_input_mask,
                    no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                    early_stopping=True,
                    max_length=self.config.decoder_max_length,
                    num_return_sequences=num_ret_seqs,
                    num_beams=self.config.num_search_samples,
                    length_penalty=1.0,  # no penalty
                    output_scores=True,
                    return_dict_in_generate=True,
                )
            else:
                question_output = self.question_model.generate(
                    input_ids=question_input_ids,
                    attention_mask=question_input_mask,
                    no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                    early_stopping=True,
                    max_length=self.config.decoder_max_length,
                    num_return_sequences=num_ret_seqs,
                    num_beams=self.config.num_search_samples,
                    length_penalty=1.0,  # no penalty
                    output_scores=True,
                    return_dict_in_generate=True,
                )
            question_predictions = question_output.sequences
            question_log_ps = question_output.sequences_scores

        question_predictions_str = self.question_tokenizer.batch_decode(
            question_predictions, skip_special_tokens=True
        )

        question_predictions_str = [
            remove_prefix(pred, "question: ") for pred in question_predictions_str
        ]

        new_articles = []
        for i in range(len(question_predictions_str)):
            new_article = (
                "relation: "
                + batch["entity_relations"][i // num_ret_seqs]
                + " question: "
                + question_predictions_str[i]
                + " context: "
                + batch["passages"][i // num_ret_seqs]
                + " </s>"
            )
            new_articles.append(new_article)

        answer_inputs = self.answer_tokenizer(
            new_articles,
            truncation=True,
            padding="max_length",
            max_length=self.config.source_max_length,
            add_special_tokens=False,
            return_tensors="pt",
        )
        answer_input_ids = answer_inputs.input_ids
        answer_input_mask = answer_inputs.attention_mask
        if self.config.gpu:
            answer_input_ids = answer_input_ids.to(current_device)
            answer_input_mask = answer_input_mask.to(current_device)

        return (
            answer_input_ids,
            answer_input_mask,
            question_input_ids,
            question_input_mask,
            question_predictions_str,
            question_log_ps,
        )

    def predict_step(self, batch, current_device):
        """Code to generate the question from the question module and then
        generate the tail entity from the response module."""
        # Free memory in GPU, very important!
        clear_cache()

        # disable dropout
        self.answer_model.eval()
        self.question_model.eval()

        (
            answer_input_ids,
            answer_input_mask,
            _,
            _,
            question_predictions_str,
            question_log_ps,
        ) = self.question_beam_predict(batch, current_device)

        second_entity_predictions = self.answer_model.generate(
            input_ids=answer_input_ids,
            attention_mask=answer_input_mask,
        )
        second_entity_predictions_str = self.answer_tokenizer.batch_decode(
            second_entity_predictions, skip_special_tokens=True
        )

        for index in range(len(second_entity_predictions_str)):
            pred_str = second_entity_predictions_str[index]
            output_batch = {
                "predictions_str": pred_str,
                "question_predictions": question_predictions_str[index],
            }
            yield output_batch

    def relation_classifier(self, batch, current_device):
        """Relation classifier using tail entity generation."""
        self.question_model.eval()
        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=-100, reduction="none")
        if self.config.gpu:
            loss_fct = loss_fct.to(current_device)
        (
            answer_input_ids,
            answer_input_mask,
            question_input_ids,
            question_input_mask,
            question_predictions_str,
            question_log_ps,
        ) = self.question_beam_predict(
            batch,
            current_device,
            with_tail_entity=False,
            num_ret_seqs=self.config.num_search_samples,
        )
        target_mask = batch["second_entity_attention_mask"]
        labels = batch["second_entity_labels"]
        if self.config.gpu:
            target_mask = target_mask.to(current_device)
            labels = labels.to(current_device)

        b_sz, dec_seq_len = labels.size()
        labels = labels.repeat(1, self.config.num_search_samples).view(-1, dec_seq_len)
        target_mask = target_mask.repeat(1, self.config.num_search_samples).view(
            -1, dec_seq_len
        )

        # Answer Computation
        with torch.no_grad():
            self.answer_model.eval()
            output = self.answer_model(
                input_ids=answer_input_ids,
                attention_mask=answer_input_mask,
                decoder_attention_mask=target_mask,
                decoder_input_ids=self.answer_model._shift_right(labels),
                labels=None,
            )

            log_p = -loss_fct(
                output.logits.view(-1, output.logits.size(-1)),
                labels.view(-1),
            )

            # b: batch size
            # sz: sequence size
            # v: vocab size
            b, sz, v = output.logits.size()
            log_p = log_p.view(b, sz)
            good_log_p = log_p.masked_fill_(labels == -100, 0.0)
            answer_log_p = torch.sum(good_log_p, dim=1).squeeze().cpu().numpy()
            question_log_ps = question_log_ps.cpu().numpy()
            for index in range(b):
                relation_log_p = answer_log_p[index] + question_log_ps[index]
                output_batch = {
                    "relation_log_p": relation_log_p,
                    "question_log_p": question_log_ps[index],
                    "answer_log_p": answer_log_p[index],
                    "generated_question": question_predictions_str[index],
                }
                yield output_batch

    def pgg_answer_training(self, batch, current_device):
        """Compute PGG loss only for the answer module."""
        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=-100, reduction="none")
        if self.config.gpu:
            loss_fct = loss_fct.to(current_device)
        (
            answer_input_ids,
            answer_input_mask,
            question_input_ids,
            question_input_mask,
            question_predictions_str,
            question_log_ps,
        ) = self.question_beam_predict(batch, current_device)
        target_mask = batch["second_entity_attention_mask"]
        labels = batch["second_entity_labels"]
        if self.config.gpu:
            target_mask = target_mask.to(current_device)
            labels = labels.to(current_device)

        # Answer Computation
        self.answer_model.train()
        output = self.answer_model(
            input_ids=answer_input_ids,
            attention_mask=answer_input_mask,
            decoder_attention_mask=target_mask,
            decoder_input_ids=self.answer_model._shift_right(labels),
            labels=None,
        )

        log_p = -loss_fct(
            output.logits.view(-1, output.logits.size(-1)),
            labels.view(-1),
        )

        # b: batch size
        # sz: sequence size
        # v: vocab size
        b, sz, v = output.logits.size()
        log_p = log_p.view(b, sz)
        good_log_p = log_p.masked_fill_(labels == -100, 0.0)
        answer_log_p = torch.sum(good_log_p, dim=1).squeeze()

        loss = -torch.mean(answer_log_p, dim=0)
        loss_value = loss.item()
        return loss, loss_value

    def response_forward(
        self, batch, new_articles, current_device, loss_fct, answer_training=False
    ):
        """Prepare the input for the response module and decide whether to
        train it for MML objectives or don't train it with PGG objective."""
        # Get output from the response module
        answer_inputs = self.answer_tokenizer(
            new_articles,
            truncation=True,
            padding="max_length",
            max_length=self.config.source_max_length,
            add_special_tokens=False,
            return_tensors="pt",
        )

        answer_input_ids = answer_inputs.input_ids
        answer_input_mask = answer_inputs.attention_mask
        if self.config.gpu:
            answer_input_ids = answer_input_ids.to(current_device)
            answer_input_mask = answer_input_mask.to(current_device)

        target_mask = batch["second_entity_attention_mask"]
        labels = batch["second_entity_labels"]
        if self.config.gpu:
            target_mask = target_mask.to(current_device)
            labels = labels.to(current_device)

        b_sz, _ = labels.size()

        (
            answer_input_ids,
            answer_input_mask,
            target_mask,
            new_labels,
        ) = prepare_response_module_input(
            answer_input_ids=answer_input_ids,
            answer_input_mask=answer_input_mask,
            labels=labels,
            target_mask=target_mask,
            num_samples=self.config.num_search_samples,
        )

        if not answer_training:
            with torch.no_grad():
                output = self.answer_model(
                    input_ids=answer_input_ids,
                    attention_mask=answer_input_mask,
                    decoder_attention_mask=target_mask,
                    decoder_input_ids=self.answer_model._shift_right(new_labels),
                    labels=None,
                )

                log_p = -loss_fct(
                    output.logits.view(-1, output.logits.size(-1)),
                    new_labels.view(-1),
                )

                b, s_len, v = output.logits.size()
                log_p = log_p.view(b, s_len)
                good_log_p = log_p.masked_fill_(new_labels == -100, 0.0)
                answer_log_p = torch.sum(good_log_p, dim=1).squeeze()
                answer_log_p = answer_log_p.view(b_sz, self.config.num_search_samples)

                return answer_log_p

        else:
            output = self.answer_model(
                input_ids=answer_input_ids,
                attention_mask=answer_input_mask,
                decoder_attention_mask=target_mask,
                decoder_input_ids=self.answer_model._shift_right(new_labels),
                labels=None,
            )

            log_p = -loss_fct(
                output.logits.view(-1, output.logits.size(-1)),
                new_labels.view(-1),
            )

            b, s_len, v = output.logits.size()
            log_p = log_p.view(b, s_len)
            good_log_p = log_p.masked_fill_(new_labels == -100, 0.0)
            answer_log_p = torch.sum(good_log_p, dim=1).squeeze()
            answer_log_p = answer_log_p.view(b_sz, self.config.num_search_samples)

            return answer_log_p

    def question_forward(
        self,
        current_device,
        question_input_ids,
        question_input_mask,
        final_sampled_question_predictions_str_reshaped,
        loss_fct,
        question_training=True,
    ):
        """Now re-run the question generator and compute the loss for the
        sampled predictions.

        This will compute the gradients in the question module.
        """

        b_sz, src_seq_len = question_input_ids.size()
        question_input_ids = question_input_ids.repeat(
            1, self.config.num_search_samples
        ).view(-1, src_seq_len)
        question_input_mask = question_input_mask.repeat(
            1, self.config.num_search_samples
        ).view(-1, src_seq_len)

        output_questions = []
        for i in range(b_sz):
            for j in range(self.config.num_search_samples):
                output_question = (
                    white_space_fix(
                        final_sampled_question_predictions_str_reshaped[i][j]
                    )
                    + " </s>"
                )
                output_questions.append(output_question)

        question_encodings = self.question_tokenizer(
            output_questions,
            truncation=True,
            padding="max_length",
            max_length=self.config.decoder_max_length,
            add_special_tokens=False,
            return_tensors="pt",
        )
        question_labels = question_encodings.pop("input_ids")
        question_target_mask = question_encodings.pop("attention_mask")

        # because HuggingFace automatically shifts the labels, the labels correspond exactly to `target_ids`.
        # We have to make sure that the PAD token is ignored

        question_labels = [
            [
                -100 if token == self.question_tokenizer.pad_token_id else token
                for token in labels
            ]
            for labels in question_labels.tolist()
        ]
        question_labels = torch.tensor(question_labels)
        if self.config.gpu:
            question_target_mask = question_target_mask.to(current_device)
            question_labels = question_labels.to(current_device)

        if question_training:
            self.question_model.train()

            _, dec_seq_len = question_labels.size()

            question_output = self.question_model(
                input_ids=question_input_ids,
                attention_mask=question_input_mask,
                decoder_attention_mask=question_target_mask,
                decoder_input_ids=self.question_model._shift_right(question_labels),
                labels=None,
            )

            log_question_p = -loss_fct(
                question_output.logits.view(-1, question_output.logits.size(-1)),
                question_labels.view(-1),
            )

            b, seq_len, v = question_output.logits.size()
            log_question_p = log_question_p.view(b, seq_len)
            good_log_question_p = log_question_p.masked_fill_(
                question_labels == -100, 0.0
            )
            question_log_p = torch.sum(good_log_question_p, dim=1).squeeze()
            question_log_p = question_log_p.view(b_sz, self.config.num_search_samples)

            return question_log_p, output_questions

        else:
            self.question_model.eval()

            _, dec_seq_len = question_labels.size()

            with torch.no_grad():
                question_output = self.question_model(
                    input_ids=question_input_ids,
                    attention_mask=question_input_mask,
                    decoder_attention_mask=question_target_mask,
                    decoder_input_ids=self.question_model._shift_right(question_labels),
                    labels=None,
                )

                log_question_p = -loss_fct(
                    question_output.logits.view(-1, question_output.logits.size(-1)),
                    question_labels.view(-1),
                )

                b, seq_len, v = question_output.logits.size()
                log_question_p = log_question_p.view(b, seq_len)
                good_log_question_p = log_question_p.masked_fill_(
                    question_labels == -100, 0.0
                )
                question_log_p = torch.sum(good_log_question_p, dim=1).squeeze()
                question_log_p = question_log_p.view(
                    b_sz, self.config.num_search_samples
                )

                return question_log_p, output_questions

    def overall_training(
        self,
        batch,
        current_device,
        sample_p=0.95,
        off_policy=True,
        answer_training=False,
        question_training=True,
        train_type="MML",
    ):
        """The main training function to decide which sampling technique to use
        and also to compute the loss corresponding to different training
        objectives."""
        loss_fct = torch.nn.CrossEntropyLoss(ignore_index=-100, reduction="none")
        if self.config.gpu:
            loss_fct = loss_fct.to(current_device)

        # Loss from the entity relation examples!
        question_input_ids = batch["entity_relation_passage_input_ids"]
        question_input_mask = batch["entity_relation_passage_attention_mask"]
        if self.config.gpu:
            question_input_ids = question_input_ids.to(current_device)
            question_input_mask = question_input_mask.to(current_device)

        if off_policy:
            # the posterior inputs also have the tail entity.
            posterier_question_input_ids = batch["posterier_input_ids"]
            posterier_question_input_mask = batch["posterier_attention_mask"]
            if self.config.gpu:
                posterier_question_input_ids = posterier_question_input_ids.to(
                    current_device
                )
                posterier_question_input_mask = posterier_question_input_mask.to(
                    current_device
                )

        b_sz, _ = question_input_ids.size()

        with torch.no_grad():
            if off_policy:
                self.init_question_model.eval()
                sampled_question_outputs = self.init_question_model.generate(
                    input_ids=posterier_question_input_ids,
                    do_sample=True,
                    no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                    max_length=self.config.decoder_max_length,
                    num_return_sequences=self.config.num_search_samples,
                    top_p=sample_p,
                    output_scores=True,
                    return_dict_in_generate=True,
                    attention_mask=posterier_question_input_mask,
                )
            else:
                self.question_model.eval()
                sampled_question_outputs = self.question_model.generate(
                    input_ids=question_input_ids,
                    do_sample=True,
                    no_repeat_ngram_size=self.config.no_repeat_ngram_size,
                    max_length=self.config.decoder_max_length,
                    num_return_sequences=self.config.num_search_samples,
                    top_p=sample_p,
                    output_scores=True,
                    return_dict_in_generate=True,
                    attention_mask=question_input_mask,
                )

            sampled_questions, question_log_ps = prob_of_sampled_predictions(
                loss_fct, sampled_question_outputs
            )

            if off_policy:
                sampled_question_predictions_str = (
                    self.init_question_tokenizer.batch_decode(
                        sampled_questions, skip_special_tokens=True
                    )
                )
            else:
                sampled_question_predictions_str = self.question_tokenizer.batch_decode(
                    sampled_questions, skip_special_tokens=True
                )

            sampled_question_predictions_str = [
                remove_prefix(pred, "question: ")
                for pred in sampled_question_predictions_str
            ]

            sampled_question_predictions_str_reshaped = [
                sampled_question_predictions_str[
                    i
                    * (self.config.num_search_samples) : (i + 1)
                    * (self.config.num_search_samples)
                ]
                for i in range(b_sz)
            ]

        sample_log_ps = question_log_ps.view(b_sz, self.config.num_search_samples)
        new_articles = []
        for i in range(b_sz):
            for j in range(self.config.num_search_samples):
                new_article = (
                    "relation: "
                    + batch["entity_relations"][i]
                    + " question: "
                    + sampled_question_predictions_str_reshaped[i][j]
                    + " context: "
                    + batch["passages"][i]
                    + " </s>"
                )
                new_articles.append(new_article)

        answer_log_p = self.response_forward(
            batch,
            new_articles,
            current_device,
            loss_fct,
            answer_training=answer_training,
        )

        question_log_p, output_questions = self.question_forward(
            current_device,
            question_input_ids,
            question_input_mask,
            sampled_question_predictions_str_reshaped,
            loss_fct,
            question_training=question_training,
        )

        if train_type == "MML":
            # easier stable way to use MML objective with backpropogation.
            if off_policy:
                ratio_log = question_log_p - sample_log_ps + answer_log_p
            else:
                ratio_log = question_log_p + answer_log_p
            easier_mml_loss = -torch.mean(torch.logsumexp(ratio_log, dim=1), dim=0)
            return easier_mml_loss

    def train_objectives(
        self,
        batch,
        current_device,
        objective_type="MML-MML-On-Sim",
        sample_p=0.95,
    ):
        """Which objective to use?"""

        # Free memory in GPU, very important!
        clear_cache()
        # Turn on training mode which enables dropout.

        if objective_type == "MML-MML-On-Sim":
            self.answer_optimizer.zero_grad()
            self.question_optimizer.zero_grad()
            self.answer_model.train()
            loss = self.overall_training(
                batch,
                current_device,
                sample_p=sample_p,
                off_policy=False,
                answer_training=True,
                question_training=True,
            )
            loss_value = loss.item()
            if not math.isnan(loss_value):
                # BackProp
                loss.backward()
                # Optimize
                self.answer_optimizer.step()
                self.question_optimizer.step()
            return loss_value

        if objective_type == "MML-MML-Off-Sim":
            self.answer_optimizer.zero_grad()
            self.question_optimizer.zero_grad()
            self.answer_model.train()
            loss = self.overall_training(
                batch,
                current_device,
                sample_p=sample_p,
                off_policy=True,
                answer_training=True,
                question_training=True,
            )
            loss_value = loss.item()
            if not math.isnan(loss_value):
                # BackProp
                loss.backward()
                # Optimize
                self.answer_optimizer.step()
                self.question_optimizer.step()
            return loss_value

        if objective_type == "MML-PGG-Off-Sim":
            self.answer_optimizer.zero_grad()
            self.question_optimizer.zero_grad()

            self.answer_model.eval()
            loss = self.overall_training(
                batch,
                current_device,
                sample_p=sample_p,
                off_policy=True,
                answer_training=False,
                question_training=True,
            )
            loss_value = loss.item()

            if not math.isnan(loss_value):
                # BackProp
                loss.backward()
                # Optimize

            self.question_model.eval()
            pgg_loss, pgg_loss_value = self.pgg_answer_training(batch, current_device)
            if not math.isnan(pgg_loss_value):
                # BackProp
                pgg_loss.backward()
                # Optimize

            self.answer_optimizer.step()
            self.question_optimizer.step()
            return (loss_value, pgg_loss_value)

        if objective_type == "MML-PGG-On-Sim":
            self.answer_optimizer.zero_grad()
            self.question_optimizer.zero_grad()

            self.answer_model.eval()
            loss = self.overall_training(
                batch,
                current_device,
                sample_p=sample_p,
                off_policy=False,
                answer_training=False,
                question_training=True,
            )
            loss_value = loss.item()

            if not math.isnan(loss_value):
                # BackProp
                loss.backward()
                # Optimize

            self.question_model.eval()
            pgg_loss, pgg_loss_value = self.pgg_answer_training(batch, current_device)
            if not math.isnan(pgg_loss_value):
                # BackProp
                pgg_loss.backward()
                # Optimize

            self.answer_optimizer.step()
            self.question_optimizer.step()
            return (loss_value, pgg_loss_value)
