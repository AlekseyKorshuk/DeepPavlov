# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import time
from logging import getLogger
from string import punctuation
from typing import List, Dict, Tuple
from collections import defaultdict

import numpy as np
import pymorphy2
import faiss
from nltk.corpus import stopwords
from nltk import sent_tokenize
from sklearn.feature_extraction.text import TfidfVectorizer
from bert_dp.tokenization import FullTokenizer

from deeppavlov.core.common.registry import register
from deeppavlov.core.models.component import Component
from deeppavlov.core.common.chainer import Chainer
from deeppavlov.core.models.serializable import Serializable
from deeppavlov.core.commands.utils import expand_path
from deeppavlov.core.common.file import load_pickle, save_pickle
from deeppavlov.models.kbqa.entity_detection_parser import EntityDetectionParser
from deeppavlov.models.kbqa.rel_ranking_bert_infer import RelRankerBertInfer

log = getLogger(__name__)


@register('ner_chunker')
class NerChunker(Component):
    """
        Class to split documents into chunks of max_chunk_len symbols so that the length will not exceed
        maximal sequence length to feed into BERT
    """

    def __init__(self, vocab_file: str, max_seq_len: int = 400, max_chunk_len: int = 180,
                 batch_size: int = 30, **kwargs):
        """

        Args:
            max_chunk_len: maximal length of chunks into which the document is split
            batch_size: how many chunks are in batch
        """
        self.max_seq_len = max_seq_len
        self.max_chunk_len = max_chunk_len
        self.batch_size = batch_size
        self.re_tokenizer = re.compile(r"[\w']+|[^\w ]")
        vocab_file = str(expand_path(vocab_file))
        self.tokenizer = FullTokenizer(vocab_file=vocab_file, do_lower_case=False)
        self.punct_ext = punctuation + " " + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        self.russian_letters = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"

    def __call__(self, docs_batch: List[str]) -> Tuple[List[List[str]], List[List[int]]]:
        """
        This method splits each document in the batch into chunks wuth the maximal length of max_chunk_len
 
        Args:
            docs_batch: batch of documents

        Returns:
            batch of lists of document chunks for each document
            batch of lists of numbers of documents which correspond to chunks
        """
        text_batch_list = []
        text_batch = []
        nums_batch_list = []
        nums_batch = []
        sentences_offsets_batch_list = []
        sentences_offsets_batch = []
        sentences_offsets_list = []
        sentences_batch_list = []
        sentences_batch = []
        sentences_list = []
        text = ""
        cur_len = 0
        cur_chunk_len = 0
        for n, doc in enumerate(docs_batch):
            start = 0
            text = ""
            sentences_list = []
            sentences_offsets_list = []
            cur_len = 0
            doc_pieces = doc.split("\n")
            doc_pieces = [self.sanitize(doc_piece) for doc_piece in doc_pieces]
            doc_pieces = [doc_piece for doc_piece in doc_pieces if len(doc_piece) > 1]
            sentences = []
            for doc_piece in doc_pieces:
                sentences += sent_tokenize(doc_piece)
            for sentence in sentences:
                cur_chunk_len = 0
                sentence_tokens = re.findall(self.re_tokenizer, sentence)
                sentence_len = sum([len(self.tokenizer.tokenize(token)) for token in sentence_tokens])
                if cur_len + sentence_len < self.max_seq_len:
                    text += f"{sentence} "
                    cur_len += sentence_len
                    end = start + len(sentence)
                    sentences_offsets_list.append((start, end))
                    sentences_list.append(sentence)
                    start = end + 1
                else:
                    text = text.strip()
                    if text:
                        text_batch.append(text)
                        sentences_offsets_batch.append(sentences_offsets_list)
                        sentences_batch.append(sentences_list)
                        nums_batch.append(n)
                    
                    if sentence_len < self.max_seq_len:
                        text = f"{sentence} "
                        cur_len = sentence_len
                        start = 0
                        end = start + len(sentence)
                        sentences_offsets_list = [(start, end)]
                        sentences_list = [sentence]
                        start = end + 1
                    else:
                        if "," in sentence:
                            sentence_chunks = sentence.split(", ")
                            for chunk in sentence_chunks:
                                chunk_tokens = re.findall(self.re_tokenizer, chunk)
                                chunk_len = sum([len(self.tokenizer.tokenize(token)) for token in chunk_tokens])
                                if cur_chunk_len + chunk_len < self.max_seq_len:
                                    text += f"{chunk}, "
                                    cur_chunk_len += chunk_len + 1
                                    end = start + len(chunk) + 1
                                    sentences_offsets_list.append((start, end))
                                    sentences_list.append(chunk)
                                    start = end + 1
                                else:
                                    text = text.strip().strip(",")
                                    if text:
                                        text_batch.append(text)
                                        sentences_offsets_batch.append(sentences_offsets_list)
                                        sentences_batch.append(sentences_list)
                                        nums_batch.append(n)
                                        
                                    chunk = " ".join(chunk.split()[:self.max_chunk_len])
                                    text = f"{chunk}, "
                                    cur_chunk_len = chunk_len
                                    start = 0
                                    end = start + len(chunk)
                                    sentences_offsets_list = [(start, end)]
                                    sentences_list = [sentence]
                                    start = end + 1
                        else:
                            chunk_tokens = sentence.split()
                            num_chunks = len(chunk_tokens) // self.max_chunk_len + int(len(chunk_tokens) % self.max_chunk > 0)
                            for ii in range(num_chunks):
                                chunk_tokens_elem = chunk_tokens[ii*self.max_chunk_len:(ii+1)*self.max_chunk_len]
                                text_batch.append(" ".join(chunk_tokens_elem))
                                sentences_offsets_batch.append([(0, len(chunk_tokens_elem))])
                                sentences_batch.append([chunk_tokens_elem])
                                nums_batch.append(n)
                
            text = text.strip().strip(",")                
            if text:
                text_batch.append(text)
                nums_batch.append(n)
                sentences_offsets_batch.append(sentences_offsets_list)
                sentences_batch.append(sentences_list)
                        
        num_batches = len(text_batch) // self.batch_size + int(len(text_batch) % self.batch_size > 0)
        for jj in range(num_batches):
            text_batch_list.append(text_batch[jj*self.batch_size:(jj+1)*self.batch_size])
            nums_batch_list.append(nums_batch[jj*self.batch_size:(jj+1)*self.batch_size])
            sentences_offsets_batch_list.append(sentences_offsets_batch[jj*self.batch_size:(jj+1)*self.batch_size])
            sentences_batch_list.append(sentences_batch[jj*self.batch_size:(jj+1)*self.batch_size])

        return text_batch_list, nums_batch_list, sentences_offsets_batch_list, sentences_batch_list

    def sanitize(self, text):
        text_len = len(text)
        
        if text_len > 0 and text[text_len - 1] not in {'.', '!', '?'}:
            i = text_len - 1
            while text[i] in self.punct_ext and i > 0:
                i -= 1
                if (text[i] in {'.', '!', '?'} and text[i - 1].lower() in self.russian_letters) or \
                        (i > 1 and text[i] in {'.', '!', '?'} and text[i - 1] in '"' and text[
                            i - 2].lower() in self.russian_letters):
                    break

            text = text[:i + 1]
        text = re.sub(r'\s+', ' ', text)
        return text


@register('entity_linker')
class EntityLinker(Component, Serializable):
    """
        Class for linking of entity substrings in the document to entities in Wikidata
    """

    def __init__(self, load_path: str,
                 word_to_idlist_filename: str,
                 entities_ranking_filename: str,
                 entities_types_sets_filename: str,
                 vectorizer_filename: str,
                 faiss_index_filename: str,
                 chunker: NerChunker = None,
                 ner: Chainer = None,
                 ner_parser: EntityDetectionParser = None,
                 entity_ranker: RelRankerBertInfer = None,
                 num_faiss_candidate_entities: int = 20,
                 num_entities_for_bert_ranking: int = 50,
                 num_faiss_cells: int = 50,
                 use_gpu: bool = True,
                 save_path: str = None,
                 fit_vectorizer: bool = False,
                 max_tfidf_features: int = 1000,
                 include_mention: bool = False,
                 ngram_range: List[int] = None,
                 num_entities_to_return: int = 10,
                 lang: str = "ru",
                 use_descriptions: bool = True,
                 return_confidences: bool = False,
                 lemmatize: bool = False,
                 **kwargs) -> None:
        """

        Args:
            load_path: path to folder with inverted index files
            word_to_idlist_filename: file with dict of words (keys) and entity ids list as value
            entities_ranking_filename: file with dict of entity ids (keys) and number of relations in Wikidata
                for entities
            entities_types_sets_filename: file with entities split into sets of PER, LOC, ORG entity types
            vectorizer_filename: filename with TfidfVectorizer data
            faiss_index_filename: file with Faiss index of words
            chunker: component deeppavlov.models.kbqa.ner_chunker
            ner: config for entity detection
            ner_parser: component deeppavlov.models.kbqa.entity_detection_parser
            entity_ranker: component deeppavlov.models.kbqa.rel_ranking_bert_infer
            num_faiss_candidate_entities: number of nearest neighbors for the entity substring from the text
            num_entities_for_bert_ranking: number of candidate entities for BERT ranking using description and context
            num_faiss_cells: number of Voronoi cells for Faiss index
            use_gpu: whether to use GPU for faster search of candidate entities
            save_path: path to folder with inverted index files
            fit_vectorizer: whether to build index with Faiss library
            max_tfidf_features: maximal number of features for TfidfVectorizer
            include_mention: whether to leave entity mention in the context (during BERT ranking)
            ngram_range: char ngrams range for TfidfVectorizer
            num_entities_to_return: number of candidate entities for the substring which are returned
            lang: russian or english
            use_description: whether to perform entity ranking by context and description
            return_confidences: whether to return confidences of entities
            lemmatize: whether to lemmatize tokens
            **kwargs:
        """
        super().__init__(save_path=save_path, load_path=load_path)
        self.morph = pymorphy2.MorphAnalyzer()
        self.lemmatize = lemmatize
        self.word_to_idlist_filename = word_to_idlist_filename
        self.entities_ranking_filename = entities_ranking_filename
        self.entities_types_sets_filename = entities_types_sets_filename
        self.vectorizer_filename = vectorizer_filename
        self.faiss_index_filename = faiss_index_filename
        self.num_entities_for_bert_ranking = num_entities_for_bert_ranking
        self.num_faiss_candidate_entities = num_faiss_candidate_entities
        self.num_faiss_cells = num_faiss_cells
        self.use_gpu = use_gpu
        self.chunker = chunker
        self.ner = ner
        self.ner_parser = ner_parser
        self.entity_ranker = entity_ranker
        self.fit_vectorizer = fit_vectorizer
        self.max_tfidf_features = max_tfidf_features
        self.include_mention = include_mention
        self.ngram_range = ngram_range
        self.num_entities_to_return = num_entities_to_return
        self.lang_str = f"@{lang}"
        if self.lang_str == "@en":
            self.stopwords = set(stopwords.words("english"))
        elif self.lang_str == "@ru":
            self.stopwords = set(stopwords.words("russian"))
        self.not_found_tokens = ["ооо", "оао", "фгуп", "муп", "акционерное общество", "зао", "мкп"]
        self.not_found_str = "not in wiki"
        self.use_descriptions = use_descriptions
        self.return_confidences = return_confidences

        self.load()

        if self.fit_vectorizer:
            self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=tuple(self.ngram_range),
                                              max_features=self.max_tfidf_features, max_df=0.85)
            self.vectorizer.fit(self.word_list)
            self.matrix = self.vectorizer.transform(self.word_list)
            self.dense_matrix = self.matrix.toarray()
            if self.num_faiss_cells > 1:
                quantizer = faiss.IndexFlatIP(self.max_tfidf_features)
                self.faiss_index = faiss.IndexIVFFlat(quantizer, self.max_tfidf_features, self.num_faiss_cells)
                self.faiss_index.train(self.dense_matrix.astype(np.float32))
            else:
                self.faiss_index = faiss.IndexFlatIP(self.max_tfidf_features)
            self.faiss_index.add(self.dense_matrix.astype(np.float32))
            self.save_vectorizers_data()
            if self.use_gpu:
                res = faiss.StandardGpuResources()
                self.faiss_index = faiss.index_cpu_to_gpu(res, 0, self.faiss_index)

    def load(self) -> None:
        self.word_to_idlist = load_pickle(self.load_path / self.word_to_idlist_filename)
        self.word_list = list(self.word_to_idlist.keys())
        self.entities_ranking_dict = load_pickle(self.load_path / self.entities_ranking_filename)
        self.entities_types_sets = load_pickle(self.load_path / self.entities_types_sets_filename)
        if not self.fit_vectorizer:
            self.vectorizer = load_pickle(self.load_path / self.vectorizer_filename)
            self.faiss_index = faiss.read_index(str(expand_path(self.faiss_index_filename)))
            if self.use_gpu:
                res = faiss.StandardGpuResources()
                self.faiss_index = faiss.index_cpu_to_gpu(res, 0, self.faiss_index)

    def save(self) -> None:
        pass

    def save_vectorizers_data(self) -> None:
        save_pickle(self.vectorizer, self.save_path / self.vectorizer_filename)
        faiss.write_index(self.faiss_index, str(expand_path(self.faiss_index_filename)))

    def __call__(self, docs_batch: List[str]):
        """

        Args:
            docs_batch: batch of documents
        Returns:
            batch of lists of candidate entity ids
            batch of lists of candidate entity ids confidences
        """
        text_batch_list, nums_batch_list, sentences_offsets_batch_list, sentences_batch_list = self.chunker(docs_batch)
        entity_ids_batch_list = []
        conf_batch_list = []
        entity_substr_batch_list = []
        tags_batch_list = []
        entity_positions_batch_list = []
        text_len_batch_list = []
        for text_batch, sentences_offsets_batch, sentences_batch in \
                zip(text_batch_list, sentences_offsets_batch_list, sentences_batch_list):
            tm_ner_st = time.time()
            ner_tokens_batch, ner_tokens_offsets_batch, ner_probas_batch = self.ner(text_batch)
            entity_substr_batch, _, entity_positions_batch = self.ner_parser(ner_tokens_batch, ner_probas_batch)
            tm_ner_end = time.time()
            log.debug(f"ner time {tm_ner_end - tm_ner_st}")
            log.debug(f"entity_substr_batch {entity_substr_batch}")
            log.debug(f"entity_positions_batch {entity_positions_batch}")
            entity_substr_tags_batch = [[(entity_substr.lower(), tag)
                                         for tag, entity_substr_list in entity_substr_dict.items()
                                         for entity_substr in entity_substr_list]
                                        for entity_substr_dict in entity_substr_batch]
            entity_substr_batch = []
            tags_batch = []
            for entity_substr_tags in entity_substr_tags_batch:
                if entity_substr_tags:
                    entity_substr_list, tags_list = zip(*entity_substr_tags)
                else:
                    entity_substr_list, tags_list = [], []
                entity_substr_batch.append(entity_substr_list)
                tags_batch.append(tags_list)

            entity_positions_batch = [[entity_positions for tag, entity_positions_list in entity_positions_dict.items()
                                       for entity_positions in entity_positions_list]
                                      for entity_positions_dict in entity_positions_batch]
            log.debug(f"entity_substr_batch {entity_substr_batch}")
            log.debug(f"entity_positions_batch {entity_positions_batch}")

            nf_entity_substr_batch, nf_tags_batch, nf_entity_positions_batch = [], [], []
            nf_entity_ids_batch, nf_conf_batch = [], []
            fnd_entity_substr_batch, fnd_tags_batch, fnd_entity_positions_batch = [], [], []

            for entity_substr_list, tags_list, entity_positions_list in \
                    zip(entity_substr_batch, tags_batch, entity_positions_batch):
                nf_entity_substr_list, nf_tags_list, nf_entity_positions_list = [], [], []
                nf_entity_ids_list, nf_conf_list = [], []
                fnd_entity_substr_list, fnd_tags_list, fnd_entity_positions_list = [], [], []
                for entity_substr, tag, entity_positions in zip(entity_substr_list, tags_list, entity_positions_list):
                    nf = False
                    for tok in self.not_found_tokens:
                        if tok in entity_substr:
                            nf = True
                            break
                    if nf:
                        nf_entity_substr_list.append(entity_substr)
                        nf_tags_list.append(tag)
                        nf_entity_positions_list.append(entity_positions)
                        if self.num_entities_to_return == 1:
                            nf_entity_ids_list.append(self.not_found_str)
                            nf_conf_list.append((0.0, 0, 0.0))
                        else:
                            nf_entity_ids_list.append([self.not_found_str])
                            nf_conf_list.append([(0.0, 0, 0.0)])
                    else:
                        fnd_entity_substr_list.append(entity_substr)
                        fnd_tags_list.append(tag)
                        fnd_entity_positions_list.append(entity_positions)
                nf_entity_substr_batch.append(nf_entity_substr_list)
                nf_tags_batch.append(nf_tags_list)
                nf_entity_positions_batch.append(nf_entity_positions_list)
                nf_entity_ids_batch.append(nf_entity_ids_list)
                nf_conf_batch.append(nf_conf_list)
                fnd_entity_substr_batch.append(fnd_entity_substr_list)
                fnd_tags_batch.append(fnd_tags_list)
                fnd_entity_positions_batch.append(fnd_entity_positions_list)

            fnd_entity_ids_batch, fnd_conf_batch = \
                self.link_entities(fnd_entity_substr_batch, fnd_tags_batch, fnd_entity_positions_batch, sentences_batch,
                                   sentences_offsets_batch, ner_tokens_offsets_batch)

            entity_substr_batch, tags_batch, entity_positions_batch, entity_ids_batch, conf_batch = [], [], [], [], []
            for i in range(len(nf_entity_substr_batch)):
                entity_substr_list = nf_entity_substr_batch[i] + fnd_entity_substr_batch[i]
                tags_list = nf_tags_batch[i] + fnd_tags_batch[i]
                entity_positions_list = nf_entity_positions_batch[i] + fnd_entity_positions_batch[i]
                entity_ids_list = nf_entity_ids_batch[i] + fnd_entity_ids_batch[i]
                conf_list = nf_conf_batch[i] + fnd_conf_batch[i]
                entity_substr_batch.append(entity_substr_list)
                tags_batch.append(tags_list)
                entity_positions_batch.append(entity_positions_list)
                entity_ids_batch.append(entity_ids_list)
                conf_batch.append(conf_list)

            entity_ids_batch_list.append(entity_ids_batch)
            conf_batch_list.append(conf_batch)
            entity_substr_batch_list.append(entity_substr_batch)
            tags_batch_list.append(tags_batch)
            entity_positions_batch_list.append(entity_positions_batch)
            text_len_batch_list.append([len(text) for text in ner_tokens_batch])

        doc_entity_ids_batch, doc_conf_batch, doc_entity_substr_batch = [], [], []
        doc_tags_batch, doc_entity_positions_batch = [], []
        doc_entity_ids, doc_conf, doc_entity_substr = [], [], []
        doc_tags, doc_entity_positions = [], []
        cur_doc_num = 0
        text_len_sum = 0
        for entity_ids_batch, conf_batch, entity_substr_batch, tags_batch, entity_positions_batch, \
            text_len_batch, nums_batch in \
                zip(entity_ids_batch_list, conf_batch_list, entity_substr_batch_list, tags_batch_list,
                    entity_positions_batch_list,
                    text_len_batch_list, nums_batch_list):
            for entity_ids, conf, entity_substr, tag, entity_positions, text_len, doc_num in \
                    zip(entity_ids_batch, conf_batch, entity_substr_batch, tags_batch,
                        entity_positions_batch, text_len_batch, nums_batch):
                if doc_num == cur_doc_num:
                    doc_entity_ids += entity_ids
                    doc_conf += conf
                    doc_entity_substr += entity_substr
                    doc_tags += tag
                    doc_entity_positions += [[pos + text_len_sum for pos in entity_position]
                                             for entity_position in entity_positions]
                    text_len_sum += text_len
                else:
                    doc_entity_ids_batch.append(doc_entity_ids)
                    doc_conf_batch.append(doc_conf)
                    doc_entity_substr_batch.append(doc_entity_substr)
                    doc_tags_batch.append(doc_tags)
                    doc_entity_positions_batch.append(doc_entity_positions)
                    doc_entity_ids = entity_ids
                    doc_conf = conf
                    doc_entity_substr = entity_substr
                    doc_tags = tag
                    doc_entity_positions = entity_positions
                    cur_doc_num = doc_num
                    text_len_sum = 0
        doc_entity_ids_batch.append(doc_entity_ids)
        doc_conf_batch.append(doc_conf)
        doc_entity_substr_batch.append(doc_entity_substr)
        doc_tags_batch.append(doc_tags)
        doc_entity_positions_batch.append(doc_entity_positions)

        if self.return_confidences:
            return doc_entity_substr_batch, doc_conf_batch, doc_entity_positions_batch, doc_entity_ids_batch
        else:
            return doc_entity_substr_batch, doc_entity_positions_batch, doc_entity_ids_batch

    def link_entities(self, entity_substr_batch: List[str], tags_batch: List[str],
                      entity_positions_batch: List[List[int]],
                      sentences_batch: List[List[str]],
                      sentences_offsets_batch: List[List[Tuple[int, int]]],
                      ner_tokens_offsets_batch) -> List[List[List[Tuple[int, int]]]]:
        log.debug(f"entity substr batch {entity_substr_batch}")
        log.debug(f"entity positions batch {entity_positions_batch}")
        entity_substr_batch = [[[word for word in entity_substr.split(' ')
                                 if word not in self.stopwords and len(word) > 0]
                                for entity_substr in entity_substr_list]
                               for entity_substr_list in entity_substr_batch]
        words_doc_nums = []
        word_count = 0
        indices_batch = []
        word_counts_batch = []
        word_tags_batch = []
        for doc_num, (entity_substr_list, tags_list) in enumerate(zip(entity_substr_batch, tags_batch)):
            indices = []
            word_counts = []
            word_tags = []
            for i, (entity_substr, tag) in enumerate(zip(entity_substr_list, tags_list)):
                for word in entity_substr:
                    words_doc_nums.append((word, doc_num))
                    indices.append(i)
                    word_counts.append(word_count)
                    word_tags.append(tag)
                    morph_parsed_word = self.morph_parse(word)
                    if word != morph_parsed_word:
                        words_doc_nums.append((morph_parsed_word, doc_num))
                        indices.append(i)
                        word_counts.append(word_count)
                        word_tags.append(tag)
                    word_count += 1
            indices_batch.append(indices)
            word_counts_batch.append(word_counts)
            word_tags_batch.append(word_tags)
        log.debug(f"words, indices, tags {words_doc_nums}")
        words, doc_nums = zip(*words_doc_nums)
        words = list(words)
        doc_nums = list(doc_nums)
        log.debug(
            f"words {words} doc_nums {doc_nums} word counts batch {word_counts_batch} tags batch {word_tags_batch}")
        ent_substr_tfidfs = self.vectorizer.transform(words).toarray().astype(np.float32)
        D_all, I_all = self.faiss_index.search(ent_substr_tfidfs, self.num_faiss_candidate_entities)
        D_batch, I_batch = [], []
        D_list, I_list = [], []
        prev_doc_num = 0
        for D, I, doc_num in zip(D_all, I_all, doc_nums):
            if D_list and doc_num != prev_doc_num:
                D_batch.append(D_list)
                I_batch.append(I_list)
                D_list, I_list = [], []
            D_list.append(D)
            I_list.append(I)
            prev_doc_num = doc_num
        if D_list:
            D_batch.append(D_list)
            I_batch.append(I_list)

        entity_ids_batch = []
        conf_batch = []
        for entity_substr_list, entity_positions_list, sentences_list, sentences_offsets_list, \
            ner_tokens_offsets_list, indices, word_counts, tags, D, I in \
                zip(entity_substr_batch, entity_positions_batch, sentences_batch, sentences_offsets_batch,
                    ner_tokens_offsets_batch, indices_batch, word_counts_batch, word_tags_batch, D_batch, I_batch):
            entity_ids_list, conf_list = [], []
            if entity_substr_list:
                tm_ind_st = time.time()
                substr_lens = [len(entity_substr) for entity_substr in entity_substr_list]
                candidate_entities_dict = defaultdict(list)
                prev_word_count = 0
                prev_index = 0
                candidate_entities = {}
                for ind_list, scores_list, index, word_count, tag in zip(I, D, indices, word_counts, tags):
                    if self.num_faiss_cells > 1:
                        scores_list = [1.0 - score for score in scores_list]
                    if word_count != prev_word_count:
                        if candidate_entities:
                            candidate_entities_dict[prev_index] += [(entity, cand_entity_len, score)
                                                                    for (entity, cand_entity_len), score in
                                                                    candidate_entities.items()]
                        candidate_entities = {}

                    for ind, score in zip(ind_list, scores_list):
                        entities_set = self.word_to_idlist[self.word_list[ind]]
                        entities_set = {entity for entity in entities_set if (entity[0] in self.entities_types_sets[tag]
                                                                              or entity[0] in self.entities_types_sets[
                                                                                  "AMB"])}
                        for entity in entities_set:
                            if entity in candidate_entities:
                                if score > candidate_entities[entity]:
                                    candidate_entities[entity] = score
                            else:
                                candidate_entities[entity] = score
                    prev_index = index
                    prev_word_count = word_count
                    debug_words = [(self.word_list[ind], score) for ind, score in zip(ind_list[:10], scores_list[:10])]
                    log.debug(f"{index} candidate_entities {debug_words}")
                if candidate_entities:
                    candidate_entities_dict[index] += [(entity, cand_entity_len, score)
                                                       for (entity, cand_entity_len), score in
                                                       candidate_entities.items()]

                candidate_entities_total = candidate_entities_dict.values()
                candidate_entities_total = [self.sum_scores(candidate_entities, substr_len)
                                            for candidate_entities, substr_len in
                                            zip(candidate_entities_total, substr_lens)]
                log.debug(f"length candidate entities list {len(candidate_entities_total)}")
                candidate_entities_list = []
                entities_scores_list = []
                for candidate_entities in candidate_entities_total:
                    log.debug(f"candidate_entities before ranking {candidate_entities[:10]}")
                    candidate_entities = [candidate_entity + (self.entities_ranking_dict.get(candidate_entity[0], 0),)
                                          for candidate_entity in candidate_entities]
                    candidate_entities = sorted(candidate_entities, key=lambda x: (x[1], x[2]), reverse=True)
                    log.debug(f"candidate_entities {candidate_entities[:10]}")
                    entities_scores = {entity: (substr_score, pop_score)
                                       for entity, substr_score, pop_score in candidate_entities}
                    candidate_entities = [candidate_entity[0] for candidate_entity
                                          in candidate_entities][:self.num_entities_for_bert_ranking]
                    conf = [candidate_entity[1:] for candidate_entity
                            in candidate_entities][:self.num_entities_for_bert_ranking]
                    log.debug(f"candidate_entities {candidate_entities[:10]}")
                    candidate_entities_list.append(candidate_entities)
                    if self.num_entities_to_return == 1 and candidate_entities:
                        entity_ids_list.append(candidate_entities[0])
                        conf_list.append(conf[0])
                    else:
                        entity_ids_list.append(candidate_entities[:self.num_entities_to_return])
                        conf_list.append(conf[:self.num_entities_to_return])
                    entities_scores_list.append(entities_scores)
                tm_ind_end = time.time()
                log.debug(f"search by index time {tm_ind_end - tm_ind_st}")
                tm_descr_st = time.time()
                if self.use_descriptions:
                    entity_ids_list, conf_list = self.rank_by_description(entity_positions_list,
                                                                          candidate_entities_list,
                                                                          entities_scores_list, sentences_list,
                                                                          sentences_offsets_list,
                                                                          ner_tokens_offsets_list)
                tm_descr_end = time.time()
                log.debug(f"description time {tm_descr_end - tm_descr_st}")
            entity_ids_batch.append(entity_ids_list)
            conf_batch.append(conf_list)

        return entity_ids_batch, conf_batch

    def morph_parse(self, word):
        morph_parse_tok = self.morph.parse(word)[0]
        if morph_parse_tok.tag.POS in {"NOUN", "ADJ", "ADJF"}:
            normal_form = morph_parse_tok.inflect({"nomn"}).word
        else:
            normal_form = morph_parse_tok.normal_form
        return normal_form

    def sum_scores(self, candidate_entities: List[Tuple[str, int]], substr_len: int) -> List[Tuple[str, float]]:
        entities_with_scores_sum = defaultdict(int)
        for entity in candidate_entities:
            entities_with_scores_sum[(entity[0], entity[1])] += entity[2]

        entities_with_scores = {}
        for (entity, cand_entity_len), scores_sum in entities_with_scores_sum.items():
            score = min(scores_sum, cand_entity_len) / max(substr_len, cand_entity_len)
            if entity in entities_with_scores:
                if score > entities_with_scores[entity]:
                    entities_with_scores[entity] = score
            else:
                entities_with_scores[entity] = score
        entities_with_scores = list(entities_with_scores.items())

        return entities_with_scores

    def rank_by_description(self, entity_positions_list: List[List[int]],
                            candidate_entities_list: List[List[str]],
                            entities_scores_list: List[Dict[str, Tuple[int, float]]],
                            sentences_list: List[str],
                            sentences_offsets_list: List[Tuple[int, int]],
                            ner_tokens_offsets_list: List[List[Tuple[int, int]]]) -> List[List[str]]:
        log.debug(f"rank, entity pos {entity_positions_list}")
        log.debug(f"rank, sentences_list {sentences_list}")
        log.debug(f"rank, sent offsets {sentences_offsets_list}")
        log.debug(f"rank, ner tokens {ner_tokens_offsets_list}")
        entity_ids_list = []
        conf_list = []
        contexts = []
        for entity_pos, candidate_entities in zip(entity_positions_list, candidate_entities_list):
            log.debug(f"entity_pos {entity_pos}")
            log.debug(f"candidate_entities {candidate_entities[:10]}")
            entity_start_offset = ner_tokens_offsets_list[entity_pos[0]][0]
            entity_end_offset = ner_tokens_offsets_list[entity_pos[-1]][1]
            sentence = ""
            rel_start_offset = 0
            rel_end_offset = 0
            for num, (sent, (sent_start_offset, sent_end_offset)) in \
                    enumerate(zip(sentences_list, sentences_offsets_list)):
                if entity_start_offset >= sent_start_offset and entity_end_offset <= sent_end_offset:
                    sentence = sent
                    rel_start_offset = entity_start_offset - sent_start_offset
                    rel_end_offset = entity_end_offset - sent_start_offset
                    break
            log.debug(f"rank, found sentence {sentence}")
            log.debug(f"rank, relative offsets {rel_start_offset}, {rel_end_offset}")
            if self.include_mention:
                context = sentence[:rel_start_offset] + "[ENT]" + sentence[rel_start_offset:rel_end_offset] + \
                          "[ENT]" + sentence[rel_end_offset:]
            else:
                context = sentence[:rel_start_offset] + "[ENT]" + sentence[rel_end_offset:]
            log.debug(f"rank, context: {context}")
            contexts.append(context)

        scores_list = self.entity_ranker.batch_rank_rels(contexts, candidate_entities_list)

        for candidate_entities, entities_scores, scores in \
                zip(candidate_entities_list, entities_scores_list, scores_list):
            log.debug(f"len candidate entities {len(candidate_entities)}")
            entities_with_scores = [(entity, round(entities_scores[entity][0], 2), entities_scores[entity][1],
                                     round(score, 2)) for entity, score in scores]
            log.debug(f"len entities with scores {len(entities_with_scores)}")
            entities_with_scores = [entity for entity in entities_with_scores if entity[3] > 0.001 if
                                    entity[0].startswith("Q")]
            entities_with_scores = sorted(entities_with_scores, key=lambda x: (x[1], x[3], x[2]), reverse=True)
            log.debug(f"entities_with_scores {entities_with_scores}")

            if entities_with_scores and 7.5 * entities_with_scores[0][3] > 1.0:
                top_entities = [score[0] for score in entities_with_scores]
                top_conf = [score[1:] for score in entities_with_scores]
            else:
                top_entities = [self.not_found_str]
                top_conf = [(0.0, 0, 0.0)]

            if self.num_entities_to_return == 1 and top_entities:
                entity_ids_list.append(top_entities[0])
                conf_list.append(top_conf[0])
            else:
                entity_ids_list.append(top_entities[:self.num_entities_to_return])
                conf_list.append(top_conf[:self.num_entities_to_return])
        return entity_ids_list, conf_list
