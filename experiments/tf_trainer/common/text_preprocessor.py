"""Text Preprocessor."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

from absl import flags
import numpy as np
import tensorflow as tf
from tf_trainer.common import base_model
from tf_trainer.common import types
from typing import Callable, Dict, List, Optional, Tuple

FLAGS = flags.FLAGS

tf.app.flags.DEFINE_bool('is_embedding_trainable', False,
                         'Enable fine tuning of embeddings.')

class TextPreprocessor(object):
  """Text Preprocessor.

  Takes an embedding and uses it to produce a word to index mapping and an
  embedding matrix.

  Note: Due to the lack of text preprocessing functions in tensorflow, we expect 
    that the text is already preprocessed (list of words) in inference.
    In training, due to the availability of tf.py_func, we handle the preprocessing.
  """

  def __init__(self, 
               embeddings_path: str, 
               is_binary_embedding: Optional[bool] = False
               ) -> None:
    self._word_to_idx, self._embeddings_matrix, self._unknown_token, self._embedding_size = \
        TextPreprocessor._get_word_idx_and_embeddings(
            embeddings_path,
            is_binary_embedding)  # type: Tuple[Dict[str, int], np.ndarray, int]
  
  def train_preprocess_fn(self, 
                         tokenizer: Callable[[str], List[str]],
                         lowercase: Optional[bool] = True
                         ) -> Callable[[types.Tensor], types.Tensor]:

    def _tokenize(text: bytes) -> np.ndarray:
      """Converts text to a list of words.

      Args:
        text: text to tokenize (string).
        lowercase: whether to include lowercasing in preprocessing (boolean).
        tokenizer: Python function to tokenize the text on.

      Returns:
        A list of strings (words).
      """

      words = tokenizer(text.decode('utf-8'))
      if lowercase:
        words = [w.lower() for w in words]
      return np.asarray([
            self._word_to_idx.get(w, self._unknown_token)
            for w in words
        ])

    def _preprocess_fn(text: types.Tensor) -> types.Tensor:
      '''Converts a text into a list of integers.

      Args:
        text: a 0-D string Tensor.

      Returns:
        A 1-D int64 Tensor.
      '''
      words = tf.py_func(_tokenize, [text], tf.int64)
      return words

    return _preprocess_fn

  def add_embedding_to_model(self, model: base_model.BaseModel,
                             text_feature_name: str) -> base_model.BaseModel:
    """Returns a new BaseModel with an embedding layer prepended.

    Args:
      model: An existing BaseModel instance.
      text_feature_name: The name of the feature containing text.
    """
    return model.map(
        functools.partial(self.create_estimator_with_embedding,
                          text_feature_name))

  def create_estimator_with_embedding(
      self, text_feature_name: str,
      estimator: tf.estimator.Estimator) -> tf.estimator.Estimator:
    """Takes an existing estimator and prepends the embedding layers to it.

    Args:
      estimator: A predefined Estimator that expects embeddings.
      text_feature_name: The name of the feature containing the text.

    Returns:
      TF Estimator with embedding ops added.

    Note: We need to consider the case of large embeddings (see: 
      https://stackoverflow.com/questions/48217599/
      how-to-initialize-embeddings-layer-within-estimator-api/48243086#48243086).
    """
    old_model_fn = estimator.model_fn
    old_config = estimator.config
    old_params = estimator.params

    def add_init_fn_to_estimatorSpec(estimator_spec, init_fn):
      '''Add a new init_fn to the scaffold part of estimator spec.'''

      def new_init_fn(scaffold, sess):
        init_fn(scaffold, sess)
        if estimator_spec.scaffold.init_fn:
          estimator_spec.scaffold.init_fn(scaffold, sess)
      
      scaffold = tf.train.Scaffold(
          init_fn=new_init_fn,
          copy_from_scaffold=estimator_spec.scaffold)
      estimator_spec_with_scaffold = tf.estimator.EstimatorSpec(
          mode=estimator_spec.mode,
          predictions=estimator_spec.predictions,
          loss=estimator_spec.loss,
          train_op=estimator_spec.train_op,
          eval_metric_ops=estimator_spec.eval_metric_ops,
          export_outputs=estimator_spec.export_outputs,
          training_chief_hooks=estimator_spec.training_chief_hooks,
          training_hooks=estimator_spec.training_hooks,
          scaffold=scaffold,
          evaluation_hooks=estimator_spec.evaluation_hooks,
          prediction_hooks=estimator_spec.prediction_hooks
          )
      return estimator_spec_with_scaffold

    def new_model_fn(features, labels, mode, params, config):
      """model_fn used in defining the new TF Estimator"""

      embeddings, embedding_init_fn = self.word_embeddings(
          trainable=FLAGS.is_embedding_trainable)

      text_feature = features[text_feature_name]
      word_embeddings = tf.nn.embedding_lookup(embeddings, text_feature)
      new_features = {text_feature_name: word_embeddings}

      # Fix dimensions to make Keras model output match label dims.
      if mode != tf.estimator.ModeKeys.PREDICT:
        labels = {k: tf.expand_dims(v, -1) for k, v in labels.items()}

      # TODO: Modify when embeddings are part of the model.
      estimator_spec = old_model_fn(new_features, labels, mode=mode, config=config)
      estimator_spec_with_scaffold = add_init_fn_to_estimatorSpec(
          estimator_spec,
          embedding_init_fn)

      return estimator_spec_with_scaffold

    return tf.estimator.Estimator(
        new_model_fn, config=old_config, params=old_params)

  def word_to_idx(self) -> Dict[str, int]:
    return self._word_to_idx

  def unknown_token(self) -> int:
    return self._unknown_token

  def word_embeddings(self, trainable) -> tf.Variable:
    """Get word embedding TF Variable."""

    embeddings = tf.get_variable(
        "embeddings",
        self._embeddings_matrix.shape,
        trainable=trainable)
    
    def init_fn(scaffold, sess):
        sess.run(embeddings.initializer, {embeddings.initial_value: self._embeddings_matrix})
    
    return embeddings, init_fn

  @staticmethod
  def _get_word_idx_and_embeddings(embeddings_path: str,
                                   is_binary_embedding: bool,
                                   max_words: Optional[int] = None
                                  ) -> Tuple[Dict[str, int], np.ndarray, int]:
    """Generate word to idx mapping and word embeddings numpy array.

    We have two levels of indirection (e.g. word to idx and then idx to
    embedding) which could reduce embedding size if multiple words map to the
    same idx. This is not currently a use case.

    Args:
      embeddings_path: Local, GCS, or HDFS path to embedding file. Each line
        should be a word and its vector representation separated by a space.
      max_words: The max number of words we are going to allow as part of the
        embedding.

    Returns:
      Tuple of vocab list, Numpy array of word embeddings with shape
      (vocab size, embedding size), and the unknown token.
    """
    word_to_idx = {}
    word_embeddings = []
    if is_binary_embedding:
      read_mode = 'rb'
    else:
      read_mode = 'r'
    with tf.gfile.Open(embeddings_path, read_mode) as f:
      for idx, line in enumerate(f):
        if max_words and idx >= max_words:
          break

        values = line.split()
        # Remove header when necessary.
        if len(values) == 2 and idx == 0:
          continue
        word = values[0]
        word_embedding = np.asarray(values[1:], dtype='float32')
        word_to_idx[word] = idx + 1  # Reserve first row for padding
        word_embeddings.append(word_embedding)

    # Add the padding "embedding"
    word_embeddings.insert(0, np.random.randn(len(word_embeddings[0])))

    # Convert embedding to numpy array and append the unknown word embedding,
    # which is the mean of all other embeddings.
    unknown_token = len(word_embeddings)
    try:
      embeddings_matrix = np.asarray(word_embeddings, dtype=np.float32)
    except:
      raise Exception('Embeddings can not be initialized.'
                      ' Is embedding binary = {}?'.format(is_binary_embedding))
    embeddings_matrix = np.append(
        embeddings_matrix, [embeddings_matrix.mean(axis=0)], axis=0)
    return word_to_idx, embeddings_matrix, unknown_token, len(word_embeddings[0])
