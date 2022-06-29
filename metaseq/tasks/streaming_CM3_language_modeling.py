# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
"""
Streaming Language Modeling task that loads corpora in plaintext and performs
on-the-fly tokenization.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import torch
from metaseq.data import (
    CausalMaskedDataset,
    Dictionary,
    JsonlDataset,
    PartitionedStreamingDataset,
    StreamingShuffleDataset,
    iterators,
)
from metaseq.tasks import register_task
from metaseq.tasks.streaming_language_modeling import (
    StreamingLanguageModelingConfig,
    StreamingLanguageModelingTask,
)

try:
    from tokenizers import (
        ByteLevelBPETokenizer,
        Tokenizer,
        decoders,
        models,
        normalizers,
        pre_tokenizers,
    )
    from tokenizers.pre_tokenizers import ByteLevel, Digits

    has_hf_tokenizers = True
except ImportError:
    has_hf_tokenizers = False


logger = logging.getLogger(__name__)

IMAGE_PREFIX = "I"
SPEECH_PREFIX = "S"


@dataclass
class StreamingCM3LanguageModelingConfig(StreamingLanguageModelingConfig):
    image_tokens: int = field(
        default=8192,
        metadata={"help": "total number of vision tokens used"},
    )
    image_length: int = field(
        default=1024,
        metadata={"help": "total number of tokens per single image"},
    )
    speech_tokens: int = field(
        default=512,
        metadata={"help": "total number of speech tokens used"},
    )
    num_sentinel_tokens: int = field(
        default=512,
        metadata={"help": "number of special sentinel tokens to add to the vocabulary"},
    )
    lambda_sentinel_tokens: int = field(
        default=1,
        metadata={"help": "poisson lambda for the cm3 objective"},
    )
    spm_path: Optional[str] = field(
        default=None, metadata={"help": "path to the HF spm vocab"}
    )
    causal_only: bool = field(
        default=False, metadata={"help": "do only causal modeling"}
    )
    # TODO: Armen: make this structured enum instead of using strings
    dataset_type: str = field(
        default="all",
        metadata={
            "help": "what type of dataset to enforce. Useful for doing ablations. Possible values: all|image"
        },
    )


@register_task(
    "streaming_CM3_language_modeling", dataclass=StreamingCM3LanguageModelingConfig
)
class StreamingCM3LanguageModelingTask(StreamingLanguageModelingTask):
    def _tokenize_one_json_imageonly(self, json):
        text = json["text"]
        text = text.split()
        text_with_spaces = ""
        for x in text:
            assert x[0] == IMAGE_PREFIX, "Expected every token to contain image prefix."
            text_with_spaces += x + " "
        tensor = torch.LongTensor(
            self.tokenizer.encode(text_with_spaces).ids + [self.eod]
        )
        assert (
            tensor.size(0) == self.args.image_length + 1
        ), f"Expected image size to be {self.args.image_length + 1} but got {tensor.size(0)}"
        return tensor

    def _initialize_gpt2_tokenizer(self, args):
        tokenizer = ByteLevelBPETokenizer.from_file(
            args.vocab_filename, args.merges_filename
        )
        tokenizer.add_special_tokens(
            [f"{IMAGE_PREFIX}{x} " for x in range(args.image_tokens)]
        )
        tokenizer.add_special_tokens(
            [f"{SPEECH_PREFIX}{x} " for x in range(args.speech_tokens)]
        )
        tokenizer.add_special_tokens(self.sentinel_tokens)
        tokenizer.add_special_tokens([self.sentinel_end])

        return tokenizer

    def _initialize_unigram_tokenizer(self, args):
        tokenizer = Tokenizer(models.Unigram()).from_file(args.spm_path)
        tokenizer.normalizer = normalizers.NFKC()
        tokenizer.pre_tokenizer = pre_tokenizers.Sequence(
            [ByteLevel(), Digits(individual_digits=True)]
        )
        tokenizer.decoder = decoders.ByteLevel()
        return tokenizer

    def _initialize_metaseq_dictionary(self, args):
        dictionary = Dictionary()
        tok_vocab_size = self.tokenizer.get_vocab_size()

        for id in range(dictionary.nspecial, tok_vocab_size):
            dictionary.add_symbol(self.tokenizer.id_to_token(id))
        final_vocab_size = args.final_vocab_size
        # final_vocab_size = 51200 for roberta dictionary
        if final_vocab_size is not None:
            if final_vocab_size < tok_vocab_size:
                raise ValueError(
                    f"incompatible: {final_vocab_size}, tok_vocab_size: {tok_vocab_size}"
                )
            dictionary.pad_to_multiple_(final_vocab_size)
        else:
            dictionary.pad_to_multiple_(8)

        self.dictionary = dictionary

    def _initialize_eod(self, args):
        self.eod = self.tokenizer.token_to_id(args.end_of_document_symbol)
        if self.eod is None:
            # This will be executed for old models that do not have the args.end_of_document_symbol explicitly set
            # and do not use <s/> (the default) but <EOS>
            self.eod = self.tokenizer.token_to_id("<EOS>")

    def _check_tokenizer_dictionary_invariants(self, args):
        assert (
            self.eod is not None
        ), "Cannot find end-of-document symbol ({}) in tokenizer".format(
            args.end_of_document_symbol
        )

        assert self.dictionary.bos_index == 0
        assert self.tokenizer.id_to_token(0) in {"<BOS>", "<s>"}
        assert self.dictionary.pad_index == 1
        assert self.tokenizer.id_to_token(1) in {"<PAD>", "<pad>"}
        assert self.dictionary.eos_index == 2
        assert self.tokenizer.id_to_token(2) in {"<EOS>", "</s>"}
        assert self.dictionary.unk_index == 3
        assert self.tokenizer.id_to_token(3) in {"<UNK>", "<unk>"}

        assert len(self.dictionary) == self.tokenizer.get_vocab_size()
        for token in self.sentinel_tokens + [self.sentinel_end]:
            assert self.tokenizer.token_to_id(token) != 3
            assert self.dictionary.index(token) != 3

        for i in range(args.image_tokens):
            token = f"{IMAGE_PREFIX}{i} "
            assert self.tokenizer.token_to_id(token) != 3
            assert self.dictionary.index(token) != 3

        for i in range(args.speech_tokens):
            token = f"{SPEECH_PREFIX}{i} "
            assert self.tokenizer.token_to_id(token) != 3
            assert self.dictionary.index(token) != 3

        assert len(self.tokenizer.encode("I1234 I1 I56 ").ids) == 3
        if args.spm_path:
            samp_tokens = self.tokenizer.encode("1234").ids
            assert (
                len(samp_tokens) == 5
            ), f"expect digit splitting for unigram tokenizer got {samp_tokens}"

        if args.spm_path:
            n = len(self.dictionary)
            assert (
                n & (n - 1) == 0
            ), "expect dictionary size for unigram tokenizer to be an exact power of two"

    def __init__(self, args):
        self.args = args
        self.datasets = {}
        self.dataset_to_epoch_iter = {}

        if not has_hf_tokenizers:
            raise ImportError("Please install tokenizers with: pip install tokenizers")

        if max(args.update_freq) > 1:
            raise NotImplementedError(
                "--update-freq is not compatible with StreamingLanguageModelingTask"
            )

        self.sentinel_end = "<eoss>"
        self.sentinel_tokens = [
            f"<sentinel:{i}>" for i in range(args.num_sentinel_tokens)
        ]

        if args.spm_path is None or args.spm_path == "":
            logger.warn(
                "By default, CM3 should be using unigram tokenization. "
                "Please double check tokenization unless you really are sure of what you are doing."
            )
            self.tokenizer = self._initialize_gpt2_tokenizer(args)
        else:
            self.tokenizer = self._initialize_unigram_tokenizer(args)

        self._initialize_metaseq_dictionary(args)
        self._initialize_eod(args)
        self._check_tokenizer_dictionary_invariants(args)
        logger.info(f"Dictionary Size: {len(self.dictionary)}")
        # confirm that metaseq dictionary and BPE have matching special symbols

        self.criterion_weights = torch.ones(len(self.dictionary))
        self.sentinel_tokens_ind = []
        for token in self.sentinel_tokens:
            token_index = self.dictionary.index(token)
            assert token_index != self.dictionary.unk_index
            self.sentinel_tokens_ind.append(token_index)
            self.criterion_weights[token_index] = 0.0

        self.sentinel_end_ind = self.dictionary.index(self.sentinel_end)

    def load_dataset(self, split: str, epoch=1, combine=False, **kwargs):
        # This function reads a bunch of jsonl files, concats them together,
        # shuffles them, then chunks them into blocks of tokens (e.g., 2048).

        # determine number of shards for this split
        cur_shard_str = self.get_shard_str(epoch, split)

        # concatenate any jsonl files that are part of the shard
        datasets, corpora = [], []
        for file in sorted(
            os.listdir(os.path.join(self.args.data, split, cur_shard_str))
        ):
            if not file.endswith(".jsonl"):
                continue
            tokenizer_func: Optional[Callable] = None
            if self.args.dataset_type == "all":
                tokenizer_func = self._tokenize_one_json
            elif self.args.dataset_type == "image":
                tokenizer_func = self._tokenize_one_json_imageonly
            else:
                raise ValueError(f"Expected all|image but got {self.args.dataset_type}")
            datasets.append(
                JsonlDataset(
                    path=os.path.join(self.args.data, split, cur_shard_str, file),
                    tokenizer=tokenizer_func,
                )
            )
            corpora.append(os.path.splitext(file)[0])
        assert len(datasets) > 0

        if self.args.multicorpus_sampling_alpha != 1:
            datasets = self._alpha_sampling(datasets, corpora, epoch)

        dataset = torch.utils.data.ConcatDataset(datasets)

        # shuffle order across epochs
        dataset = StreamingShuffleDataset(dataset, seed=self.args.seed)

        # chunk into blocks of tokens
        self.datasets[split] = CausalMaskedDataset(
            self.args.lambda_sentinel_tokens,
            self.sentinel_tokens_ind,
            "causal" if self.args.causal_only else "poisson",
            self.args.tokens_per_sample + 1,
            self.sentinel_end_ind,
            dataset,
            # We generate blocks with one extra token, so that we have a target
            # for the final input token. This results in slight data loss.
            block_size=self.args.tokens_per_sample + 1,
            break_mode=self.args.sample_break_mode,
            # we drop the remainder block during training
            drop_last=(split == "train"),
            padding_idx=self.source_dictionary.pad(),
            # 1284 is a randomly-generated offset to decouple the seed used here
            # from the seed used above in StreamingShuffleDataset
            seed=1284 + self.args.seed,
        )

    def get_batch_iterator(
        self,
        dataset,
        max_tokens=None,
        max_sentences=None,
        max_positions=None,
        ignore_invalid_inputs=False,
        required_batch_size_multiple=1,
        seed=1,
        num_shards=1,
        shard_id=0,
        num_workers=0,
        epoch=1,
        data_buffer_size=0,
        disable_iterator_cache=False,
        batch_by_size=True,
        skip_remainder_batch=False,
    ):
        """
        Get an iterator that yields batches of data from the given dataset.

        Args:
            dataset (torch.utils.data.Dataset): dataset to batch
            max_sentences (int, optional): max number of sentences in each
                batch (default: None).
            num_shards (int, optional): shard the data iterator into N
                shards (default: 1).
            shard_id (int, optional): which shard of the data iterator to
                return (default: 0).
            num_workers (int, optional): how many subprocesses to use for data
                loading. 0 means the data will be loaded in the main process
                (default: 0).
            epoch (int, optional): the epoch to start the iterator from
                (default: 1).
            data_buffer_size (int, optional): number of batches to
                preload (default: 0).
            disable_iterator_cache (bool, optional): don't cache the
                EpochBatchIterator
                (default: False).
            batch_by_size (bool, optional):
                batch sequences of similar length together to reduce padding.
                If false, each batch will be of size max_sentences.
            skip_remainder_batch (bool, optional): if set, discard the last
                batch in each training epoch, as the last batch is often smaller
                than local_batch_size * distributed_word_size (default: ``True``).
        Returns:
            ~metaseq.iterators.EpochBatchIterator: a batched iterator over the
                given dataset split
        """
        assert max_tokens is None

        # Up to this point, we have shuffled documents, flattened them into a 1D
        # tensor, then chunked into token blocks. But if documents are long, then
        # adjacent blocks may be from a single document, and naively distributed
        # sequential blocks to GPUs may cause entire updates to be dominated by a
        # handful of unique documents. Instead we have a readahead buffer that
        # reads in 10 full batches of data and shuffles sequences across them,
        # thus increasing randomness. This assumes that no single document spans
        # 10 full batches, which is reasonable when batch sizes are in the
        # millions and documents are on average much smaller.
        assert isinstance(dataset, CausalMaskedDataset)
        shuffle_buffer_size = 10 * max_sentences * num_shards
        logger.info(f"setting shuffle buffer size to {shuffle_buffer_size}")
        dataset.set_shuffle_buffer_size(shuffle_buffer_size)

        # partition dataset across data parallel workers
        dataset = PartitionedStreamingDataset(
            dataset,
            num_shards=num_shards,
            shard_id=shard_id,
            drop_last=skip_remainder_batch,
        )

        # create a stateful/checkpointable iterator for the current data
        # parallel worker
        return iterators.StreamingEpochBatchIterator(
            dataset=dataset,
            batch_size=max_sentences,
            collate_fn=self._collate_fn,
            drop_last=skip_remainder_batch,
            num_workers=num_workers,
            epoch=epoch,
        )
