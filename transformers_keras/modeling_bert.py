import json
import logging
import os

import tensorflow as tf

from .layers import MultiHeadAttention
from .modeling_utils import choose_activation, initialize, parse_pretrained_model_files
from transformers_keras.adapters.abstract_adapter import zip_weights, parse_pretrained_model_files
from transformers_keras.adapters.bert_adapter import BertAdapter


def unpack_input_ids_and_segment_ids(inputs):
    if not isinstance(inputs, (list, tuple)):
        raise ValueError('Invalid inputs type! Inputs type must be a list or tuple!')
    inputs = list(inputs)
    if len(inputs) == 0:
        raise ValueError('Invalid inputs, must be not empty!')
    if len(inputs) == 1:
        input_ids, segment_ids = inputs[0], None
    if len(inputs) == 2:
        input_ids, segment_ids = inputs[0], inputs[1]
    if segment_ids is None:
        segment_ids = tf.cast(tf.fill(tf.shape(input_ids), 0), dtype=tf.int32)
    return input_ids, segment_ids


def unpack_input_ids_and_segment_ids_and_mask(inputs):
    if not isinstance(inputs, (list, tuple)):
        raise ValueError('Invalid inputs type! Inputs type must be a list or tuple!')
    inputs = list(inputs)
    if len(inputs) == 0:
        raise ValueError('Invalid inputs, must be not empty!')
    if len(inputs) == 1:
        input_ids, segment_ids, mask = inputs[0], None, None
    if len(inputs) == 2:
        input_ids, segment_ids, mask = inputs[0], inputs[1], None
    if len(inputs) == 3:
        input_ids, segment_ids, mask = inputs[0], inputs[1], inputs[2]
    if segment_ids is None:
        segment_ids = tf.cast(tf.fill(tf.shape(input_ids), 0), dtype=tf.int32)
    if mask is None:
        mask = tf.cast(tf.greater(input_ids, 0), dtype=tf.int32)
    return input_ids, segment_ids, mask


class BertEmbedding(tf.keras.layers.Layer):

    def __init__(self,
                 vocab_size=1,
                 max_positions=512,
                 embedding_size=768,
                 type_vocab_size=2,
                 dropout_rate=0.2,
                 stddev=0.02,
                 epsilon=1e-12,
                 **kwargs):
        super().__init__(**kwargs)
        assert vocab_size > 0, "vocab_size must greater than 0."
        self.vocab_size = vocab_size
        self.embedding_size = embedding_size
        self.stddev = stddev

        self.position_embedding = tf.keras.layers.Embedding(
            max_positions,
            embedding_size,
            embeddings_initializer=initialize(stddev),
            name='position_embedding'
        )
        self.token_type_embedding = tf.keras.layers.Embedding(
            type_vocab_size,
            embedding_size,
            embeddings_initializer=initialize(stddev),
            name='token_type_embedding'
        )
        self.layer_norm = tf.keras.layers.LayerNormalization(epsilon=epsilon, name='layer_norm')
        self.dropout = tf.keras.layers.Dropout(dropout_rate)

    def build(self, input_shape):
        self.token_embedding = self.add_weight(
            'weight',
            shape=[self.vocab_size, self.embedding_size],
            initializer=initialize(self.stddev)
        )
        super().build(input_shape)

    def call(self, inputs, mode='embedding', training=None):
        # used for masked lm
        if mode == 'linear':
            return tf.matmul(inputs, self.token_embedding, transpose_b=True)

        input_ids, token_type_ids = unpack_input_ids_and_segment_ids(inputs)
        seq_len = tf.shape(input_ids)[1]
        position_ids = tf.range(seq_len, dtype=tf.int32)[tf.newaxis, :]

        position_embeddings = self.position_embedding(position_ids)
        token_type_embeddings = self.token_type_embedding(token_type_ids)
        token_embeddings = tf.gather(self.token_embedding, input_ids)

        embeddings = token_embeddings + token_type_embeddings + position_embeddings
        embeddings = self.layer_norm(embeddings)
        embeddings = self.dropout(embeddings, training=training)
        return embeddings


class BertIntermediate(tf.keras.layers.Layer):

    def __init__(self, intermediate_size=3072, activation='gelu', stddev=0.02, **kwargs):
        super().__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(
            intermediate_size, kernel_initializer=initialize(stddev), name='dense')
        self.activation = choose_activation(activation)

    def call(self, inputs, training=None):
        hidden_states = inputs
        hidden_states = self.dense(hidden_states)
        hidden_states = self.activation(hidden_states)
        return hidden_states


class BertEncoderLayer(tf.keras.layers.Layer):

    def __init__(self,
                 hidden_size=768,
                 num_attention_heads=8,
                 intermediate_size=3072,
                 activation='gelu',
                 hidden_dropout_rate=0.2,
                 attention_dropout_rate=0.1,
                 epsilon=1e-12,
                 stddev=0.02,
                 **kwargs):
        super().__init__(**kwargs)
        # attention block
        self.attention = MultiHeadAttention(
            hidden_size=hidden_size, 
            num_attention_heads=num_attention_heads, 
            hidden_dropout_rate=hidden_dropout_rate, 
            attention_dropout_rate=attention_dropout_rate, 
            name='attention')
        # intermediate block
        self.intermediate = BertIntermediate(
                intermediate_size=intermediate_size, 
                activation=activation, 
                stddev=stddev, 
                name='intermediate')
        # output block
        self.output_dense = tf.keras.layers.Dense(hidden_size, kernel_initializer=initialize(stddev), name='dense')
        self.output_dropout = tf.keras.layers.Dropout(hidden_dropout_rate)
        self.output_layer_norm = tf.keras.layers.LayerNormalization(epsilon=epsilon, name='layer_norm')

    def call(self, inputs, training=None):
        hidden_states, mask = inputs
        attn_output, attn_weights = self.attention(inputs=(hidden_states, hidden_states, hidden_states, mask))
        outputs = self.intermediate(inputs=attn_output)
        outputs = self.output_dropout(self.output_dense(outputs), training=training)
        outputs = self.output_layer_norm(attn_output + outputs)
        return outputs, attn_weights


class BertEncoder(tf.keras.layers.Layer):

    def __init__(self,
                 num_layers=6,
                 hidden_size=768,
                 num_attention_heads=8,
                 intermediate_size=3072,
                 activation='gelu',
                 hidden_dropout_rate=0.2,
                 attention_dropout_rate=0.1,
                 epsilon=1e-12,
                 stddev=0.02,
                 **kwargs):
        super().__init__(**kwargs)
        self.encoder_layers = [
            BertEncoderLayer(
                hidden_size=hidden_size,
                num_attention_heads=num_attention_heads,
                intermediate_size=intermediate_size,
                activation=activation,
                hidden_dropout_rate=hidden_dropout_rate,
                attention_dropout_rate=attention_dropout_rate,
                epsilon=epsilon,
                stddev=stddev,
                name='layer_{}'.format(i)
            ) for i in range(num_layers)
        ]

    def call(self, inputs, training=None):
        hidden_states, attention_mask = inputs
        all_hidden_states = []
        all_attention_scores = []
        for _, encoder in enumerate(self.encoder_layers):
            hidden_states, attention_score = encoder(inputs=(hidden_states, attention_mask))
            all_hidden_states.append(hidden_states)
            all_attention_scores.append(attention_score)

        return hidden_states, all_hidden_states, all_attention_scores

 
class BertPooler(tf.keras.layers.Layer):

    def __init__(self, hidden_size=768, stddev=0.02, **kwargs):
        super().__init__(**kwargs)
        self.dense = tf.keras.layers.Dense(
            hidden_size, kernel_initializer=initialize(stddev), activation='tanh', name='dense')

    def call(self, inputs, training=None):
        hidden_states = inputs
        # pool the first token: [CLS]
        outputs = self.dense(hidden_states[:, 0])
        return outputs


class Bert(tf.keras.Model):

    def __init__(self,
                 vocab_size=1,
                 max_positions=512,
                 hidden_size=768,
                 type_vocab_size=2,
                 num_layers=6,
                 num_attention_heads=8,
                 intermediate_size=3072,
                 activation='gelu',
                 hidden_dropout_rate=0.2,
                 attention_dropout_rate=0.1,
                 stddev=0.02,
                 epsilon=1e-12,
                 **kwargs):
        kwargs.pop('name', None)
        super().__init__(name='bert', **kwargs)
        self.bert_embedding = BertEmbedding(
            vocab_size=vocab_size,
            max_positions=max_positions,
            embedding_size=hidden_size,
            type_vocab_size=type_vocab_size,
            dropout_rate=hidden_dropout_rate,
            stddev=stddev,
            epsilon=epsilon,
            name='embedding')

        self.bert_encoder = BertEncoder(
            num_layers=num_layers,
            hidden_size=hidden_size,
            num_attention_heads=num_attention_heads,
            intermediate_size=intermediate_size,
            activation=activation,
            hidden_dropout_rate=hidden_dropout_rate,
            attention_dropout_rate=attention_dropout_rate,
            epsilon=epsilon,
            stddev=stddev,
            name='encoder')

        self.bert_pooler = BertPooler(hidden_size=hidden_size, stddev=stddev, name='pooler')

    def call(self, inputs, training=None):
        input_ids, token_type_ids, mask = unpack_input_ids_and_segment_ids_and_mask(inputs)
        mask = mask[:, tf.newaxis, tf.newaxis, :]  # (batch_size, seq_len) -> (batch_size, 1, 1, seq_len)
        embedding = self.bert_embedding(inputs=(input_ids, token_type_ids), mode='embedding')
        output, all_hidden_states, all_attention_scores = self.bert_encoder(inputs=(embedding, mask))
        pooled_output = self.bert_pooler(output)
        return output, pooled_output, all_hidden_states, all_attention_scores
     
    def dummy_inputs(self):
        input_ids = tf.constant([0] * 128, dtype=tf.int64, shape=(1, 128))
        segment_ids = tf.constant([0] * 128, dtype=tf.int64, shape=(1, 128))
        #mask = tf.constant([1] * 128, dtype=tf.int64, shape=(1, self.max_positions))
        return (input_ids, segment_ids)

    @classmethod
    def from_pretrained(cls, pretrained_model_dir, adapter=None, verbose=True, **kwargs):
        if not adapter:
            adapter = BertAdapter()
        model_config, name_mapping, ckpt, vocab_file = adapter.adapte(pretrained_model_dir)
        model = cls(**model_config)
        model(model.dummy_inputs())
        weights_values = zip_weights(model, ckpt, name_mapping, verbose=verbose)
        tf.keras.backend.batch_set_value(weights_values)
        return model


class BertMLMHead(tf.keras.layers.Layer):
    """Masked language model for BERT pre-training."""

    def __init__(self,
                 embedding,
                 vocab_size=-1,
                 hidden_size=768,
                 activation='gelu',
                 epsilon=1e-12,
                 stddev=0.02,
                 **kwargs):
        super().__init__(**kwargs)
        assert vocab_size > 0, "vocab_size must greater than 0."
        self.vocab_size = vocab_size
        self.embedding = embedding
        self.activation = choose_activation(activation)
        self.dense = tf.keras.layers.Dense(hidden_size, kernel_initializer=initialize(stddev), name='dense')
        self.layer_norm = tf.keras.layers.LayerNormalization(epsilon=epsilon, name='layer_norm')

    def build(self, input_shape):
        self.bias = self.add_weight(shape=(self.vocab_size,), initializer='zeros', trainable=True, name='bias')
        super().build(input_shape)

    def call(self, inputs, training=None):
        hidden_states = inputs
        hidden_states = self.dense(hidden_states)
        hidden_states = self.activation(hidden_states)
        hidden_states = self.layer_norm(hidden_states)
        hidden_states = self.embedding(inputs=hidden_states, mode='linear')
        hidden_states = hidden_states + self.bias
        return hidden_states


class BertNSPHead(tf.keras.layers.Layer):
    """Next sentence prediction for BERT pre-training."""

    def __init__(self, stddev=0.02, **kwargs):
        super().__init__(**kwargs)
        self.classifier = tf.keras.layers.Dense(2, kernel_initializer=initialize(stddev), name='dense')

    def call(self, inputs, training=None):
        pooled_output = inputs
        relation = self.classifier(pooled_output)
        return relation


class BertPretrainingHeads(tf.keras.layers.Layer):

    def __init__(self, embedding, vocab_size, hidden_size, activation='gelu', stddev=0.02, epsilon=1e-8, **kwargs):
        super().__init__(**kwargs)
        self.mlm = BertMLMHead(
                embedding, 
                vocab_size=vocab_size, 
                hidden_size=hidden_size, 
                activation=activation, 
                epsilon=epsilon, 
                stddev=stddev, 
                name='predictions')
        self.nsp = BertNSPHead(stddev=stddev, name='seq_relationship')

    def call(self, inputs):
        sequence_outputs, pooled_outputs = inputs
        mlm_outputs = self.mlm(sequence_outputs)
        nsp_outputs = self.nsp(pooled_outputs)
        return mlm_outputs, nsp_outputs


def BertPretrainingModel(**kwargs):
    input_ids = tf.keras.layers.Input(shape=(None,), dtype=tf.int32)
    segment_ids = tf.keras.layers.Input(shape=(None, ), dtype=tf.int32)
    bert = Bert(
            vocab_size=kwargs.get('vocab_size', -1),
            type_vocab_size=kwargs.get('type_vocab_size', 2),
            max_positions=kwargs.get('max_positions', 512),
            num_layers=kwargs.get('num_layers', 12),
            hidden_size=kwargs.get('hidden_size', 768),
            intermediate_size=kwargs.get('intermediate_size', 3072),
            activation=kwargs.get('activation', 'gelu'),
            hidden_dropout_rate=kwargs.get('hidden_dropout_rate', 0.2),
            attention_dropout_rate=kwargs.get('attention_dropout_rate', 0.1),
            stddev=kwargs.get('stddev', 0.02),
            epsilon=kwargs.get('epsilon', 1e-8),
            name='bert')
    sequence_outputs, pooled_outputs, _, _ = bert(inputs=(input_ids, segment_ids))
    heads = BertPretrainingHeads(embedding=bert.bert_embedding,
            vocab_size=kwargs.get('vocab_size', -1),
            hidden_size=kwargs.get('hidden_size', 768),
            activation=kwargs.get('kwargs', 'gelu'),
            epsilon=kwargs.get('epsilon', 1e-8),
            stddev=kwargs.get('stddev', 0.02),
            name='cls')
    mlm_outputs, nsp_outputs = heads(inputs=(sequence_outputs, pooled_outputs))

    model = tf.keras.Model(inputs=[input_ids, segment_ids], outputs=[mlm_outputs, nsp_outputs])
    model.compile(loss='sparse_categorical_crossentropy', optimizer=tf.keras.optimizers.Adam(kwargs.get('lr', 1e-5)))
    model.summary()
    return model


