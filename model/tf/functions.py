import tensorflow as tf
import numpy as np
from typing import List

# Auxiliary methods
def patches(X:tf.Tensor,
          patch_size:int,
          ):

    def patches_2d(X:tf.Tensor):
        h, w = X.shape.as_list()
        X_middle = tf.stack(tf.split(X,h//patch_size, axis = 0), axis = 0)
        X_final = tf.map_fn(fn=lambda y: tf.stack(tf.split(y,w//patch_size, axis = 1), axis = 0), elems = X_middle)
        X_final = tf.reshape(X_final, shape=[-1,patch_size,patch_size])
        return X_final

    if len(X.shape)==5:
        X = tf.squeeze(X, axis=1)
    _, h, w, _ = X.shape.as_list()
    assert h%patch_size==0, f"Patch size must divide images height"
    assert w%patch_size==0, f"Patch size must divide images width"
    X = tf.transpose(X, perm=[0,3,1,2])
    patches_tf = tf.map_fn(fn=lambda y: tf.map_fn(fn = lambda z: patches_2d(z), elems = y),
                           elems = X,
                           )
    patches_tf = tf.transpose(patches_tf, [0,2,3,4,1])
    return patches_tf

def unflatten(flattened, num_channels):
    if len(flattened.shape)==2:
        n, p = flattened.shape.as_list()
    else:
        _, n, p = flattened.shape.as_list()
    unflattened = tf.reshape(flattened, (-1, n, int(np.sqrt(p//num_channels)), int(np.sqrt(p//num_channels)), num_channels))
    return unflattened

def unpatch(x, num_channels):
    if len(x.shape) < 5:
        _, num_patches, h, w, ch = unflatten(x, num_channels).shape.as_list()
    else:
        _, num_patches, h, w, ch = x.shape.as_list()
    assert ch==num_channels, f"Num. channels must agree"
    elem_per_axis = int(np.sqrt(num_patches))
    x = tf.stack(tf.split(x, elem_per_axis, axis = 1), axis = 1)
    patches_middle = tf.concat(tf.unstack(x, axis = 2), axis = -2)
    restored_images = tf.reshape(tf.concat(tf.unstack(patches_middle, axis = 1), axis = -3), shape=[-1,1,h*elem_per_axis,w*elem_per_axis,ch])
    return restored_images

def resampling(encoded_patches, img_size:int=128, patch_size:List[int]=[16,8], num_channels:int=3):
    num_patch = (img_size//patch_size[1])**2
    proj_dim = num_channels*patch_size[1]**2
    original_image = unpatch(unflatten(encoded_patches, num_channels), num_channels)
    new_patches = patches(tf.squeeze(original_image, axis=1), patch_size[1])
    new_patches_flattened = tf.reshape(new_patches, shape=[-1, num_patch, proj_dim])
    return new_patches_flattened

# Layers
class Resampling(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int=128,
                 patch_size:List[int]=[16,8],
                 num_channels:int=1,
                 dropout:float=0.,
                 trainable:bool=True,
                 ):
        super(Resampling, self).__init__()
        # Parameters
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = [(self.img_size//patch)**2 for patch in self.patch_size]
        self.num_channels = num_channels
        self.projection_dim = [self.num_channels*patch**2 for patch in self.patch_size]
        self.trainable = trainable
        # Layers
        if trainable:
            self.BN = tf.keras.layers.BatchNormalization()
            self.LeakyReLU = tf.keras.layers.LeakyReLU()
            self.drop = tf.keras.layers.Dropout(dropout)
            if self.patch_size[0]>self.patch_size[1]:
                self.rs = tf.keras.layers.Conv2D(self.num_patches[1], (3,3), strides=(2,2), padding='same', kernel_initializer=tf.random_normal_initializer(0., 0.02), use_bias=False)
            else:
                self.rs = tf.keras.layers.Conv2DTranspose(self.num_patches[1], (3,3), strides=(2,2), padding='same', kernel_initializer=tf.random_normal_initializer(0., 0.02), use_bias=False)

    def call(self, encoded:tf.Tensor):
        if self.trainable:
            X_patch = unflatten(encoded, self.num_channels)
            X_patch = tf.transpose(X_patch, perm=[0,4,2,3,1])
            X_patch = tf.map_fn(fn=lambda y: self.rs(y), elems = X_patch)
            X_patch = self.BN(X_patch)
            X_patch = self.drop(X_patch)
            X_patch = self.LeakyReLU(X_patch)
            X_patch = tf.transpose(X_patch, perm=[0,4,2,3,1])
            return tf.reshape(X_patch, [-1, self.num_patches[1], self.projection_dim[1]])
        else:
            return resampling(encoded, self.img_size, self.patch_size, self.num_channels)


class PatchEncoder(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int=128,
                 patch_size:int=16,
                 num_channels:int=1,
                 ):
        super(PatchEncoder, self).__init__()
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = (self.img_size//self.patch_size)**2
        self.projection_dim = self.num_channels*self.patch_size**2
        self.projection = tf.keras.layers.Dense(units=self.projection_dim)
        self.position_embedding = tf.keras.layers.Embedding(
            input_dim=self.num_patches, output_dim=self.projection_dim
        )

    def call(self, X:tf.Tensor):
        X = patches(X, self.patch_size)
        positions = tf.range(start=0, limit=self.num_patches, delta=1)
        encoded = self.projection(X) + self.position_embedding(positions)
        return encoded


class DeepPatchEncoder(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int=128,
                 patch_size:List[int]=[16,8],
                 num_channels:int=1,
                 dropout:float=.2,
                 bias:bool=False,
                 ):
        super(DeepPatchEncoder, self).__init__()
        # Parameters
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = [(self.img_size//patch)**2 for patch in self.patch_size]
        self.projection_dim = [self.num_channels*patch**2 for patch in self.patch_size]
        self.kernel_size = [patch//4 if patch>2 else 2 for patch in self.patch_size]
        self.patch_size_extended = [self.img_size] + self.patch_size
        self.bias = bias
        # Layers
        self.dense = tf.keras.layers.Dense(self.projection_dim[0])
        self.position_embedding = tf.keras.layers.Embedding(
            input_dim=self.num_patches[0], output_dim=self.projection_dim[0],
        )
        ## Sub
        self.seqCNN = []
        if self.patch_size[0]>self.patch_size[-1]:
            self.strides_size = [self.patch_size_extended[i]//self.patch_size_extended[i+1] for i in range(len(self.patch_size))]
            for i in range(len(self.patch_size)-1):
                self.seqCNN.append(tf.keras.Sequential([
                                                     tf.keras.layers.Conv2D(self.num_patches[i+1],
                                                                            kernel_size=self.kernel_size[i+1],
                                                                            strides=self.strides_size[i+1],
                                                                            padding='same',
                                                                            use_bias=self.bias,
                                                                            kernel_initializer=tf.keras.initializers.RandomNormal(0, .02),
                                                                            ),
                                                     tf.keras.layers.BatchNormalization(),
                                                      ])
                                )
        else:
            self.strides_size = [self.patch_size_extended[i+1]//self.patch_size_extended[i] for i in range(len(self.patch_size))]
            for i in range(len(self.patch_size)-1):
                self.seqCNN.append(tf.keras.Sequential([
                                                     tf.keras.layers.Conv2DTranspose(self.num_patches[i+1],
                                                                                     kernel_size=self.kernel_size[i+1],
                                                                                     strides=self.strides_size[i+1],
                                                                                     padding='same',
                                                                                     use_bias=self.bias,
                                                                                     kernel_initializer=tf.keras.initializers.RandomNormal(0, .02),
                                                                                     ),
                                                     tf.keras.layers.BatchNormalization(),
                                                      ])
                                )

    def call(self, X:tf.Tensor):
        # Flat patches
        patch = patches(X,self.patch_size[0])
        encoded = tf.reshape(patch, [-1, self.num_patches[0], self.projection_dim[0]])
        # Embedding 1
        positions = tf.range(start=0, limit=self.num_patches[0], delta=1)
        positions = self.position_embedding(positions)
        encoded = encoded + positions
        # Embedding 2
        encoded = unflatten(encoded, self.num_channels)
        encoded = tf.transpose(encoded, [0,4,2,3,1])
        positions = tf.expand_dims(positions, axis = 0)
        positions = unflatten(positions, self.num_channels)
        positions = tf.transpose(positions, [0,4,2,3,1])
        for i, resample in enumerate(self.seqCNN):
            # Generate next level positional encoding
            positions = tf.map_fn(lambda y: resample(y), elems = positions)
            # Reshape encoded to add (non-trainable)
            encoded = tf.transpose(encoded, [0,4,2,3,1])
            encoded = tf.reshape(encoded, [-1, self.num_patches[i], self.projection_dim[i]])
            encoded = resampling(encoded, self.img_size, self.patch_size[i:i+2], self.num_channels)
            encoded = unflatten(encoded, self.num_channels)
            encoded = tf.transpose(encoded, [0,4,2,3,1])
            encoded =  encoded + positions
        encoded = tf.transpose(encoded, [0,4,2,3,1])
        encoded = tf.reshape(encoded, [-1, self.num_patches[-1], self.projection_dim[-1]])
        encoded = resampling(encoded, self.img_size, [self.patch_size[-1], self.patch_size[0]], self.num_channels)
        return encoded

## FeedForward
class FeedForward(tf.keras.layers.Layer):
    def __init__(self,
                 projection_dim:int,
                 hidden_dim:int,
                 dropout:float,
                 ):
        super().__init__()
        self.D1 = tf.keras.layers.Dense(hidden_dim)
        self.Drop1 = tf.keras.layers.Dropout(dropout)
        self.D2 = tf.keras.layers.Dense(projection_dim)
        self.Drop2 = tf.keras.layers.Dropout(dropout)

    def call(self, x):
        x = self.D1(x)
        x = tf.keras.activations.gelu(x)
        x = self.Drop1(x)
        x = self.D2(x)
        x = tf.keras.activations.gelu(x)
        x = self.Drop2(x)
        return x

## ReAttention
class ReAttention(tf.keras.layers.Layer):
    def __init__(self,
                 dim,
                 num_patches,
                 num_channels=1,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.2,
                 proj_drop=0.2,
                 apply_transform=True,
                 transform_scale=False,
                 ):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.num_channels = num_channels
        self.num_patches = num_patches
        self.patch_size = int(np.sqrt(dim))

        head_dim = self.dim // self.num_heads
        self.apply_transform = apply_transform
        self.scale = qk_scale or head_dim ** -0.5
        if apply_transform:
            self.reatten_matrix = tf.keras.layers.Conv2D(self.num_patches, 1)
            self.var_norm = tf.keras.layers.BatchNormalization()
            self.qconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', kernel_initializer = tf.random_normal_initializer(0., 0.02), use_bias=qkv_bias)
            self.kconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', kernel_initializer = tf.random_normal_initializer(0., 0.02), use_bias=qkv_bias)
            self.vconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', kernel_initializer = tf.random_normal_initializer(0., 0.02), use_bias=qkv_bias)
            self.reatten_scale = self.scale if transform_scale else 1.0
        else:
            self.qconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', kernel_initializer = tf.random_normal_initializer(0., 0.02), use_bias=qkv_bias)
            self.kconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', kernel_initializer = tf.random_normal_initializer(0., 0.02), use_bias=qkv_bias)
            self.vconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', kernel_initializer = tf.random_normal_initializer(0., 0.02), use_bias=qkv_bias)
        
        self.attn_drop = tf.keras.layers.Dropout(attn_drop)
        self.proj = tf.keras.layers.Dense(dim)
        self.proj_drop = tf.keras.layers.Dropout(proj_drop)
    
    def create_queries(self, x, letter):
        if letter=='q':
            x = unflatten(x, self.num_channels)
            x = tf.map_fn(fn=lambda y: tf.keras.activations.gelu(self.qconv2d(y)), elems=x)
        if letter == 'k':
            x = unflatten(x, self.num_channels)
            x = tf.map_fn(fn=lambda y: tf.keras.activations.gelu(self.kconv2d(y)), elems=x)
        if letter == 'v':
            x = unflatten(x, self.num_channels)
            x = tf.map_fn(fn=lambda y: tf.keras.activations.gelu(self.vconv2d(y)), elems=x)

        x = tf.reshape(x, shape=[-1, self.num_patches, self.dim])
        x = tf.reshape(x, shape = [-1, self.num_patches, self.num_heads, self.dim//self.num_heads, 1])
        x = tf.transpose(x, perm = [4,0,2,1,3])
        return x[0]

    def call(self, x, atten=None):
        _, N, C = x.shape.as_list()
        q = self.create_queries(x, 'q')
        k = self.create_queries(x, 'k')
        v = self.create_queries(x, 'v')
        attn = (tf.linalg.matmul(q, k, transpose_b = True)) * self.scale
        attn = tf.keras.activations.softmax(attn, axis = -1)
        attn = self.attn_drop(attn)
        if self.apply_transform:
            attn = self.var_norm(self.reatten_matrix(attn)) * self.reatten_scale
        attn_next = attn
        x = tf.reshape(tf.transpose(tf.linalg.matmul(attn, v), perm = [0,2,1,3]), shape = [-1, N, C])
        x = self.proj(x)
        x = self.proj_drop(x)
        return x, attn_next


## Transformer Encoder
class AttentionTransformerEncoder(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int,
                 patch_size:int,
                 num_channels:int,
                 num_heads:int,
                 transformer_layers:int,
                 hidden_dim:int,
                 attn_drop:float,
                 proj_drop:float,
                 ):
        super().__init__()
        # Parameters
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = (self.img_size//self.patch_size)**2
        self.projection_dim = self.num_channels*self.patch_size**2
        self.transformer_layers = transformer_layers
        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.attn_drop = attn_drop
        self.proj_drop = proj_drop
        # Layers
        self.LN1 = []
        self.LN2 = []
        self.Attn = []
        self.FF = []
        for _ in range(self.transformer_layers):
            self.LN1.append(tf.keras.layers.LayerNormalization())
            self.LN2.append(tf.keras.layers.LayerNormalization())
            self.Attn.append(
                tf.keras.layers.MultiHeadAttention(num_heads=self.num_heads,
                                                   key_dim=self.projection_dim,
                                                   dropout=self.attn_drop,
                                                  )
            )
            self.FF.append(
                FeedForward(projection_dim = self.projection_dim,
                                       hidden_dim = self.hidden_dim,
                                       dropout = self.proj_drop,
                                       )
            )

    def call(self, encoded_patches):
        for i in range(self.transformer_layers):
            encoded_patch_attn = self.Attn[i](encoded_patches, encoded_patches)
            encoded_patches = tf.keras.layers.Add()([encoded_patch_attn, encoded_patches])
            encoded_patches = self.LN1[i](encoded_patches)
            encoded_patch_FF = self.FF[i](encoded_patches)
            encoded_patches = tf.keras.layers.Add()([encoded_patch_FF, encoded_patches])
            encoded_patches = self.LN2[i](encoded_patches)
        return encoded_patches


class ReAttentionTransformerEncoder(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int,
                 patch_size:int,
                 num_channels:int,
                 num_heads:int,
                 transformer_layers:int,
                 hidden_dim:int,
                 attn_drop:float,
                 proj_drop:float,
                 ):
        super().__init__()
        # Parameters
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = (self.img_size//self.patch_size)**2
        self.projection_dim = self.num_channels*self.patch_size**2
        self.transformer_layers = transformer_layers
        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.attn_drop = attn_drop
        self.proj_drop = proj_drop
        # Layers
        self.LN1 = []
        self.LN2 = []
        self.ReAttn = []
        self.FF = []
        for _ in range(self.transformer_layers):
            self.LN1.append(tf.keras.layers.LayerNormalization())
            self.LN2.append(tf.keras.layers.LayerNormalization())
            self.ReAttn.append(
                ReAttention(dim = self.projection_dim,
                                  num_patches = self.num_patches,
                                  num_channels = self.num_channels,
                                  num_heads = self.num_heads,
                                  attn_drop = self.attn_drop,
                                  )
            )
            self.FF.append(
                FeedForward(projection_dim = self.projection_dim,
                                       hidden_dim = self.hidden_dim,
                                       dropout = self.proj_drop,
                                       )
            )

    def call(self, encoded_patches):
        for i in range(self.transformer_layers):
            encoded_patch_attn, _ = self.ReAttn[i](encoded_patches)
            encoded_patches = encoded_patch_attn + encoded_patches
            encoded_patches = self.LN1[i](encoded_patches)
            encoded_patches = self.FF[i](encoded_patches) + encoded_patches
            encoded_patches = self.LN2[i](encoded_patches)
        return encoded_patches
