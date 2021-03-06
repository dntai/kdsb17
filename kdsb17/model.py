import os

import numpy as np
from keras.layers import (Input, Conv3D, Conv3DTranspose, Dense,
                          Activation, BatchNormalization, Dropout, Concatenate, Flatten, Lambda)
from keras.models import Model
from keras.callbacks import ModelCheckpoint, EarlyStopping, CSVLogger, TensorBoard, TerminateOnNaN

from kdsb17.layers import SpatialPyramidPooling3D
from kdsb17.activations import log_softmax
from kdsb17.advanced_activations import ShiftedELU
from kdsb17.losses import build_gmd_log_likelihood
from kdsb17.callbacks import BatchLossCSVLogger
from kdsb17.utils.file import makedir


class NakedModel(object):
    """Naked, empty model class. This class implements the common facilities to compile and train a model.
    GaussianMixtureCAE and LungNet inherit from and build upon it by defining the _build_layers method.

    Args:
        optimizer (str): Name of Keras optimizer. An optimizer object can also be passed directly.
        es_patience (int): Number of patience epochs for EarlyStopping.
        model_path (str): Path to the location where the model files will be saved.
        weights_name_format(str): Format of weight checkpoint files (refer to the Keras documentation for details).
        histogram_freq (int): Frequency (in epochs) for saving weights, activations and gradients
            histograms in Tensorboard.If 0, no histograms will be computed.

    Methods:
        build_model: Builds and compiles the model.
        summary: Print summary of model to stdout
        load_weights_from_file: Load the weights of the model from a file.
        fit_generator: Train the model on data yielded batch-by-batch by a generator.
    """

    def __init__(self,
                 optimizer='adam', es_patience=10,
                 model_path='/tmp/', weights_name_format='weights.{epoch:02d}-{val_loss:.6f}.hdf5',
                 histogram_freq=0):

        self.model_path = model_path
        self.weights_name_format = weights_name_format
        self.optimizer = optimizer
        self.es_patience = es_patience
        self.histogram_freq = histogram_freq

        self._loss = None
        self._input_layer = None
        self._output_layer = None
        self._model = None

    def _build_callbacks(self):
        """Builds callbacks for training model.
        """
        makedir(self.model_path)

        checkpointer = ModelCheckpoint(filepath=os.path.join(self.model_path, self.weights_name_format),
                                       monitor='val_loss', save_best_only=True, save_weights_only=True)

        early_stopper = EarlyStopping(monitor='val_loss', min_delta=0, patience=self.es_patience)

        epoch_logger = CSVLogger(os.path.join(self.model_path, 'epoch_log.csv'))
        batch_logger = BatchLossCSVLogger(os.path.join(self.model_path, 'batch_log.csv'))

        tensorboard_path = os.path.join(self.model_path, 'tensorboard')
        tensorboard = TensorBoard(log_dir=tensorboard_path,
                                  histogram_freq=self.histogram_freq, write_grads=(self.histogram_freq > 0))

        terminator = TerminateOnNaN()

        return [checkpointer, early_stopper, epoch_logger, batch_logger, tensorboard, terminator]

    def _build_layers(self):
        pass

    def _compile_model(self):
        self._model.compile(optimizer=self.optimizer, loss=self._loss)

    def build_model(self, freeze=None):
        """Builds and compiles the model.

        Args:
            freeze (list): Name of layers to freeze in training.
        """
        self._build_layers()
        self._model = Model(self._input_layer, self._output_layer)

        # Freeze layers
        if freeze is not None:
            for layer_name in freeze:
                layer = self._model.get_layer(name=layer_name)
                layer.trainable = False
                print('%s is set to %d' % (layer_name, layer.trainable))

        self._compile_model()

    def summary(self):
        """Print summary of model to stdout.
        """
        self._model.summary()

    def load_weights_from_file(self, path):
        """Load the weights of the model from a file.

        Args:
            path (str): Path to the weights file.
        """
        self._model.load_weights(path, by_name=True)
        print('Loaded weights from file.')

    def fit_generator(self, train_generator, steps_per_epoch, epochs,
                      validation_generator, validation_steps):
        """Train the model on data yielded batch-by-batch by a generator.

        Args:
            train_generator: A data generator that yields (x, y) tuples of training data/labels.
            steps_per_epoch: Steps (number of batches) per epoch.
            epochs: Number of epochs.
            validation_generator: A data generator that yields (x, y) tuples of validation data/labels.
            validation_steps: Validation steps (number of batches).

        Returns:
            Keras History object with history of training losses.
       """

        callbacks = self._build_callbacks()

        history = self._model.fit_generator(generator=train_generator,
                                            steps_per_epoch=steps_per_epoch,
                                            epochs=epochs,
                                            callbacks=callbacks,
                                            validation_data=validation_generator,
                                            validation_steps=validation_steps)

        return history

    @staticmethod
    def _check_input_array(x):
        if x.dtype != 'float32':
            raise ValueError('Input array must be of type float32.')
        if x.ndim != 5:
            raise ValueError('Input array must have exactly 5 dimensions: (samples, z, y, z, channels)')
        if x.shape[-1] != 1:
            raise ValueError('Size of last dimension of input array must be exactly 1.')


class Encoder(object):
    """Auxiliary class to define encoding layers common to GaussianMixtureCAE and LungNet.

    Args:
        nb_filters_per_layer (tuple): Number of filters per layer.
        kernel_size (tuple): Convolution kernel size.
        padding (str): Padding type ('same' or 'valid').
        batch_normalization (bool): Whether to apply batch normalization to convolutional layers (True) or not (False).
    """

    def __init__(self, nb_filters_per_layer, kernel_size, padding, batch_normalization):
        self.nb_filters_per_layer = nb_filters_per_layer
        self.kernel_size = kernel_size
        self.padding = padding
        self.batch_normalization = batch_normalization

    def _custom_conv3d(self, layer, nb_filters, strides, name):
        # TODO: Write me a documentation

        layer = Conv3D(nb_filters, kernel_size=self.kernel_size, strides=strides,
                       padding=self.padding, name=('encoder_conv_%s' % name))(layer)

        if self.batch_normalization:
            layer = BatchNormalization(mode=0, axis=1, name=('encoder_bn_%s' % name))(layer)

        layer = Activation('relu', name=('encoder_act_%s' % name))(layer)

        return layer

    def _build_encoder_layers(self, layer):
        """Stacks sequence of conv/pool layers to make the encoder half.
        """

        for i, nb_filters in enumerate(self.nb_filters_per_layer):
            # layer = self._custom_conv3d(layer, nb_filters, (1, 1, 1), '1-1_%d' % i)
            layer = self._custom_conv3d(layer, nb_filters, (1, 1, 1), '1-2_%d' % i)
            layer = self._custom_conv3d(layer, nb_filters, (2, 2, 2), '2-1_%d' % i)

        return layer


class GaussianMixtureCAE(Encoder, NakedModel):
    """Builds and compiles a model for representation learning 3D CT lung scans:
    Performs feature learning on CT scan patches. The network structure is as follows:
        Input -> Encoder -> Decoder -> Output
        The output parametrizes a Gaussian Mixture Density.

    Args:
        n_gaussians (int): Number of Gaussians in the mixture.
        input_shape (tuple): Shape of input 3D array.
        Other arguments passed to NakedModel and Encoder instances.
    """

    def __init__(self,
                 n_gaussians, input_shape,
                 nb_filters_per_layer=(64, 128, 256), kernel_size=(3, 3, 3), padding='same', batch_normalization=False,
                 optimizer='adam', es_patience=10,
                 model_path='/tmp/', weights_name_format='weights.{epoch:02d}-{val_loss:.6f}.hdf5',
                 histogram_freq=0):

        # TODO: Is there really no way to do this nicely with super()?
        NakedModel.__init__(self, optimizer, es_patience, model_path, weights_name_format, histogram_freq)
        Encoder.__init__(self, nb_filters_per_layer, kernel_size, padding, batch_normalization)

        self.input_shape = input_shape
        self.n_gaussians = n_gaussians
        self.n_dense_log_prior = 128
        self.n_dense_sigma_sq = 128

        z, y, x = input_shape
        c = z * y * x  # Number of output dimensions
        self._loss = build_gmd_log_likelihood(c, n_gaussians)

    def _custom_conv3dtranspose(self, layer, nb_filters, strides, name):

        layer = Conv3DTranspose(nb_filters, kernel_size=self.kernel_size, strides=strides,
                                padding=self.padding, name=('decoder_conv_%s' % name))(layer)

        if self.batch_normalization:
            layer = BatchNormalization(mode=0, axis=1, name=('decoder_bn_%s' % name))(layer)

        layer = Activation('relu', name=('decoder_act_%s' % name))(layer)

        return layer

    def _build_decoder_layers(self, layer):
        """Stacks sequence of conv/up layers to make the decoder half.
        """

        for i, nb_filters in enumerate(self.nb_filters_per_layer[::-1]):
            # layer = self._custom_conv3dtranspose(layer, nb_filters, (1, 1, 1), '1-1_%d' % i)
            layer = self._custom_conv3dtranspose(layer, nb_filters, (2, 2, 2), '2-1_%d' % i)
            layer = self._custom_conv3dtranspose(layer, nb_filters, (1, 1, 1), '1-2_%d' % i)

        return layer

    def _build_gmd_layers(self, encoded, decoded):
        """Builds the 3D CAE output layer that parametrizes a Gaussian Mixture Density.
        """

        # Log-priors
        # First squeeze the filters with a convolution before flattening
        log_prior = Conv3D(1, kernel_size=self.kernel_size, padding='same', name='log_prior_conv3d')(encoded)
        log_prior = Flatten(name='log_prior_flatten')(log_prior)
        log_prior = Dense(self.n_dense_log_prior, activation='relu', name='log_prior_dense_1')(log_prior)
        log_prior = Dense(self.n_gaussians, activation=log_softmax, name='log_prior')(log_prior)

        # Means
        mu = Conv3D(self.n_gaussians, kernel_size=self.kernel_size, padding='same', name='mu_conv3d')(decoded)
        mu = Flatten(name='mu')(mu)

        # Variances
        # First squeeze the filters with a convolution before flattening
        sigma_sq = Conv3D(1, kernel_size=self.kernel_size, padding='same', name='sigma_sq_conv3d')(encoded)
        sigma_sq = Flatten(name='sigma_sq_flatten')(sigma_sq)
        sigma_sq = Dense(self.n_dense_sigma_sq, activation='relu', name='sigma_sq_dense_1')(sigma_sq)
        sigma_sq = Dense(self.n_gaussians, name='sigma_sq_dense_2')(sigma_sq)
        sigma_sq = ShiftedELU(shift=1.0, alpha=1.0, name='sigma_sq')(sigma_sq)

        gmd = Concatenate(axis=-1, name='gmd')([log_prior, sigma_sq, mu])

        return gmd

    def _build_layers(self):
        """Builds all layers
        """
        z, y, x = self.input_shape
        self._input_layer = Input(shape=(z, y, x, 1))

        # 3D Convolutional Autoencoder
        encoded = self._build_encoder_layers(self._input_layer)
        decoded = self._build_decoder_layers(encoded)

        # Output layer parametrizes a Gaussian Mixture Density.
        self._output_layer = self._build_gmd_layers(encoded, decoded)

    def predict(self, array):
        """Predict from model.

        Args:
            array (numpy.ndarray): Input array of shape (samples, z, y, x, 1) and type float32.

        Returns:
            Array of the same shape and type as the input containing the 3D CAE reconstruction.
            Each voxel y of the reconstruction is predicted as:
                y = mu[K] where K = argmax(k)(prior[k]/sigma[k]), k is the index of each gaussian in the mixture.

                Ref: Bishop C. M., Mixture Density Networks, 1994.
                https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/bishop-ncrg-94-004.pdf
        """
        self._check_input_array(array)

        pred = self._model.predict(array)

        m = self.n_gaussians
        _, z, y, x, _ = array.shape
        splits = [m, 2*m]  # numpy.split expect locations, not sizes

        # Get GMD parameters
        # Parameters are concatenated along the second axis
        log_prior, sigma_sq, mu = np.split(pred, axis=1, indices_or_sections=splits)

        mu = np.reshape(mu, (-1, z, y, x, m))

        which = (np.exp(log_prior)/np.sqrt(sigma_sq)).argmax(axis=1)
        sample, z, y, x = np.indices(array.shape[:-1])

        return np.expand_dims(mu[sample, z, y, x, which], axis=4)


class LungNet(Encoder, NakedModel):
    """Builds and compiles a set of two models for representation learning and classification of 3D CT lung scans:

    Classifies full CT lung scans, using the features learned by the GaussianMixtureCAE.
    The network structure is as follows:

    Input -> Encoder -> SpatialPyramidPooling -> Classifier -> Output

    The encoder weights are transferred from the GaussianMixtureCAE, the classifier is a stack of dense layers,
    and the output parametrizes a Bernoulli distribution on the class labels.

    Args:
        spp_nb_bins_per_level (tuple): Number of bins per level in Spatial Pyramid Pooling.
        n_dense (tuple): Number of hidden units per fully-connected layer.
            The length of the tuple determines the number of layers.
        dropout_rate (float): Dropout rate of fully-connected layers.
        Other arguments passed to NakedModel and Encoder.
    """
    def __init__(self,
                 nb_filters_per_layer=(64, 128, 256), kernel_size=(3, 3, 3), padding='same', batch_normalization=False,
                 spp_nb_bins_per_level=(1, 2, 4), n_dense=(1024, 1024), dropout_rate=0.5,
                 optimizer='adam', es_patience=10,
                 model_path='/tmp/', weights_name_format='weights.{epoch:02d}-{val_loss:.6f}.hdf5'):

        # TODO: Is there really no way to do this nicely with super()?
        NakedModel.__init__(self, optimizer, es_patience, model_path, weights_name_format)
        Encoder.__init__(self, nb_filters_per_layer, kernel_size, padding, batch_normalization)

        self.spp_nb_bins_per_level = spp_nb_bins_per_level
        self.n_dense = n_dense
        self.dropout_rate = dropout_rate

        self._loss = 'binary_crossentropy'

    def _build_classifier_layers(self, encoded):
        """Builds layers for classification on top of encoder layers.
        """

        h = SpatialPyramidPooling3D(self.spp_nb_bins_per_level, name='spp3d')(encoded)
        for n in self.n_dense:
            h = Dense(n, activation='relu')(h)
            if self.dropout_rate:
                h = Dropout(self.dropout_rate)(h)
        y = Dense(1, activation='sigmoid')(h)

        return y

    def _build_layers(self):
        """Builds all layers
        """
        self._input_layer = Input(shape=(None, None, None, 1))

        # 3D Convolutional Autoencoder
        encoded = self._build_encoder_layers(self._input_layer)

        # Classifier
        # Inherits encoder layers from 3D CAE, and performs binary classification.
        self._output_layer = self._build_classifier_layers(encoded)

    def _compile_model(self):
        self._model.compile(optimizer=self.optimizer, loss=self._loss, metrics=['accuracy'])

    def predict(self, x):
        """Predict from model.

        Args:
            x (numpy.ndarray): Input array of shape (samples, z, y, x, 1) and type float32.

        Returns:
            A scalar indicating the probability of cancer within one year.
        """
        self._check_input_array(x)

        return self._model.predict(x)
