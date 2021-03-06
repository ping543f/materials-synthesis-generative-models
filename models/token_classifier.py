import json

import numpy as np

import tensorflow as tf
from bilm import Batcher, BidirectionalLanguageModel, weight_layers
from keras import backend as K
from keras import optimizers
from keras.callbacks import Callback, EarlyStopping, ModelCheckpoint
from keras.layers import (GRU, Bidirectional, Dense, Dropout, Input,
                          TimeDistributed)
from keras.models import Model, load_model


class TokenClassifier(object):
  def __init__(self, seq_maxlen=100, vocab="vocab.txt", 
                      options="elmo_options.json", 
                      weights="elmo_weights.hdf5"):
    self.token_classes = {
      0: "null",
      1: "precursor",
      2: "target",
      3: "operation",
    }
    self.session = None
    self.X_train = None
    self.X_dev = None
    self.Y_train = None
    self.Y_dev = None

    self.inv_token_classes = {v: k for k, v in self.token_classes.items()}
    self._seq_maxlen = seq_maxlen

    self._load_tf_session()
    self._load_embeddings(vocab, options, weights)

  def build_nn_model(self, recurrent_dim=2048, dense1_dim=1024, elmo_dim=1024):
    input_vectors = Input(shape=(self._seq_maxlen, elmo_dim))
    drop_1 = Dropout(0.5)(input_vectors)
    rnn_1 = Bidirectional(GRU(recurrent_dim, return_sequences=True))(drop_1)
    drop_2 = Dropout(0.5)(rnn_1)
    dense_1 = TimeDistributed(Dense(dense1_dim, activation="relu"))(drop_2)
    drop_3 = Dropout(0.5)(dense_1)
    dense_out = TimeDistributed(Dense(len(self.token_classes), activation="softmax"))(drop_3)

    model = Model(inputs=[input_vectors], outputs=[dense_out])
    model.compile(loss='categorical_crossentropy',
                  optimizer=optimizers.Adam(lr=0.001, beta_1=0.9, beta_2=0.999, epsilon=None, decay=0.0, amsgrad=True),
                  metrics=['accuracy'])
    self.model = model
    self.fast_predict = K.function(
      self.model.inputs + [K.learning_phase()],
      [self.model.layers[-1].output]
    )

  def train(self, batch_size=256, num_epochs=30, checkpt_filepath=None, 
            checkpt_period=5, verbosity=1, val_split=0.0):
    self._load_tf_session(use_cpu=False)
    callbacks = [
      ModelCheckpoint(
        checkpt_filepath,
        monitor='val_loss',
        verbose=0,
        save_best_only=True,
        period=checkpt_period
        )
    ]

    self.model.fit(
      x=self.X_train,
      y=self.Y_train,
      batch_size=batch_size,
      epochs=num_epochs,
      validation_split=val_split,
      validation_data=(self.X_dev, self.Y_dev),
      callbacks= callbacks,
      verbose=verbosity,
    )


  def featurize_elmo_list(self, sent_toks_list, batch_size=128):
    padded_list = [t[:self._seq_maxlen] + ['']*max(self._seq_maxlen - len(t), 0) for t in sent_toks_list]

    features = []
    prev_i = 0
    for i in range(batch_size, len(sent_toks_list) + batch_size - 1, batch_size):
      context_ids = self.batcher.batch_sentences(padded_list[prev_i:i])
      elmo_features = self.session.run(
          [self.elmo_context_output['weighted_op']],
          feed_dict={self.character_ids: context_ids}
      )
      features.extend(elmo_features[0])
      prev_i = i

    return np.array(features)

  def featurize_elmo(self, sent_toks):
    length = [self._seq_maxlen]
    padded_toks = [sent_toks[:self._seq_maxlen]  + ['']*max(self._seq_maxlen - len(sent_toks), 0)]

    context_ids = self.batcher.batch_sentences(padded_toks)
    elmo_feature = self.session.run(
        [self.elmo_context_output['weighted_op']],
        feed_dict={self.character_ids: context_ids}
    )

    features = elmo_feature[0]
    return np.array(features)

  def evaluate(self, batch_size=32):
    return self.model.evaluate(self.X_test, self.Y_test, batch_size=batch_size)

  def predict_one(self, words):
    num_words = len(words)
    elmo_feature_vector = self.featurize_elmo(words)
    return [self.token_classes[np.argmax(w)] for w in self.fast_predict([elmo_feature_vector])[0].squeeze()][:num_words]

  def predict_many(self, sent_list):
    num_words = [len(s) for s in sent_list]
    elmo_feature_vectors = self.featurize_elmo_list(sent_list)
    predicted_labels = []

    for elmo_vec, sent_len in zip(elmo_feature_vectors, num_words):
      predicted_labels.append(
        [self.token_classes[np.argmax(w)] for w in self.fast_predict([[elmo_vec]])[0].squeeze()][:sent_len]
      )

    return predicted_labels

  def save(self, filepath='bin/token_classifier.model'):
    self.model.save(filepath)

  def load(self, filepath='bin/token_classifier-SNAPSHOT.model'):
    self.model = load_model(filepath)
    self.fast_predict = K.function(
      self.model.inputs + [K.learning_phase()],
      [self.model.layers[-1].output]
    )

  def _load_tf_session(self, use_cpu=False):
    if self.session is not None:
      self.session.close()

    if use_cpu:
      config = tf.ConfigProto(
        device_count={'CPU' : 1, 'GPU' : 0},
        allow_soft_placement=True,
      )
    else:
      config = tf.ConfigProto()
      config.gpu_options.allow_growth = True
    
    self.session = tf.InteractiveSession(config=config)

  def _load_embeddings(self, 
                      vocab="vocab.txt", 
                      options="elmo_options.json", 
                      weights="elmo_weights.hdf5"):
    self.elmo_model = BidirectionalLanguageModel(options, weights)
    self.batcher = Batcher(vocab, 50)

    self.character_ids = tf.placeholder('int32', shape=(None, None, 50))
    context_embeddings_op = self.elmo_model(self.character_ids)
    self.elmo_context_output = weight_layers(
      'output', context_embeddings_op, l2_coef=0.0
    )

    tf.global_variables_initializer().run()
