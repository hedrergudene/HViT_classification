import tensorflow as tf
import numpy as np
from typing import List

# Auxiliary methods
def patch(X:tf.Tensor,
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
    # Alberto: Added to reconstruct from bs, n, projection_dim -> bs, n, c, h, w
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

def resampling(encoded_patches, num_patches:List[int]=[64,256], projection_dim:List[int]=[196,64], num_channels:int=1):
    new_patch_size = int(np.sqrt(projection_dim[1]))
    original_image = unpatch(unflatten(encoded_patches, num_channels), num_channels)
    new_patches = Patches(new_patch_size)(tf.squeeze(original_image, axis=1))
    new_patches_flattened = tf.reshape(new_patches, shape=[-1, num_patches[1], projection_dim[1]])
    return new_patches_flattened

# Layers
class Patches(tf.keras.layers.Layer):
    def __init__(self, patch_size):
        super(Patches, self).__init__()
        self.patch_size = patch_size

    def get_config(self):
        config = super().get_config().copy()
        config.update({
            'patch_size': self.patch_size,
        })
        return config

    def call(self, images):
        batch_size = tf.shape(images)[0]
        patches = tf.image.extract_patches(
            images=images,
            sizes=[1, self.patch_size, self.patch_size, 1],
            strides=[1, self.patch_size, self.patch_size, 1],
            rates=[1, 1, 1, 1],
            padding="VALID",
        )
        patch_dims = patches.shape[-1]
        patches = tf.reshape(patches, [batch_size, -1, patch_dims])
        return patches

class DeepPatchEncoder(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int=128,
                 patch_size:List[int]=[16,8],
                 num_channels:int=1,
                 ):
        super(DeepPatchEncoder, self).__init__()
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = [(self.img_size//patch)**2 for patch in self.patch_size]
        self.projection_dim = [self.num_channels*patch**2 for patch in self.patch_size]
        self.dense = tf.keras.layers.Dense(self.projection_dim[0])
        if self.patch_size[0]>self.patch_size[1]:
            self.position_embedding = tf.keras.layers.Embedding(
                input_dim=self.num_patches[1], output_dim=self.projection_dim[1],
            )
        else:
            self.position_embedding = tf.keras.layers.Embedding(
                input_dim=self.num_patches[0], output_dim=self.projection_dim[0],
            )

    def call(self, X:tf.Tensor):
        if self.patch_size[0]>self.patch_size[1]: # If it's downsampling
            patch = Patches(self.patch_size[1])(X)
            flat = tf.reshape(patch, [-1, self.num_patches[1], self.projection_dim[1]])
            positions = tf.range(start=0, limit=self.num_patches[1], delta=1)
            encoded = flat + self.position_embedding(positions)
            restored = unpatch(unflatten(encoded, 1), 1)
            restored = Patches(self.patch_size[0])(tf.squeeze(restored, axis = 1))
            restored = tf.reshape(restored, [-1, self.num_patches[0], self.projection_dim[0]])
            restored = self.dense(restored)
            return restored
        else: # If it's upsampling
            patch = Patches(self.patch_size[0])(X)
            flat = tf.reshape(patch, [-1, self.num_patches[0], self.projection_dim[0]])
            positions = tf.range(start=0, limit=self.num_patches[0], delta=1)
            encoded = flat + self.position_embedding(positions)
            restored = self.dense(encoded)
            return restored


class Resampling(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int=128,
                 patch_size:List[int]=[16,8],
                 num_channels:int=1,
                 dropout=.2,
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
            self.dropout = dropout
            self.Conv2D_pre = tf.keras.layers.Conv2D(self.num_patches[0], (3,3), strides=(1,1), padding='same')
            if self.patch_size[0]>self.patch_size[1]:
                self.rs = tf.keras.layers.Conv2D(self.num_patches[1], (3,3), strides=(2,2), padding='same')
            else:
                self.rs = tf.keras.layers.Conv2DTranspose(self.num_patches[1], (3,3), strides=(2,2), padding='same')
            self.Conv2D_post = tf.keras.layers.Conv2D(self.num_patches[1], (3,3), strides=(1,1), padding='same')

    def call(self, encoded:tf.Tensor):
        if self.trainable:
            X_patch = unflatten(encoded, self.num_channels)
            X_patch = tf.transpose(X_patch, perm=[0,4,2,3,1])
            X_patch = tf.map_fn(fn=lambda y: tf.keras.activations.gelu(self.Conv2D_pre(y)), elems = X_patch)
            X_patch = tf.keras.layers.Dropout(self.dropout)(X_patch)
            X_patch = tf.map_fn(fn=lambda y: tf.keras.activations.gelu(self.rs(y)), elems = X_patch)
            X_patch = tf.keras.layers.Dropout(self.dropout)(X_patch)
            X_patch = tf.map_fn(fn=lambda y: tf.keras.activations.gelu(self.Conv2D_post(y)), elems = X_patch)
            X_patch = tf.keras.layers.Dropout(self.dropout)(X_patch)        
            X_patch = tf.transpose(X_patch, perm=[0,4,2,3,1])
            return tf.reshape(X_patch, [-1, self.num_patches[1], self.projection_dim[1]])
        else:
            return resampling(encoded, self.num_patches, self.projection_dim, self.num_channels)

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
                 qkv_bias=True,
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
            self.qconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', use_bias=qkv_bias)
            self.kconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', use_bias=qkv_bias)
            self.vconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', use_bias=qkv_bias)
            self.reatten_scale = self.scale if transform_scale else 1.0
        else:
            self.qconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', use_bias=qkv_bias)
            self.kconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', use_bias=qkv_bias)
            self.vconv2d = tf.keras.layers.Conv2D(self.num_channels,3,padding = 'same', use_bias=qkv_bias)
        
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
                 attn_drop:int,
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
                                       dropout = self.attn_drop,
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
                 attn_drop:int,
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
                                       dropout = self.attn_drop,
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

class HViT(tf.keras.layers.Layer):
    def __init__(self,
                 img_size:int=128,
                 patch_size:List[int]=[16,8],
                 num_channels:int=1,
                 num_heads:int=8,
                 transformer_layers:List[int]=[5,5],
                 hidden_unit_factor:float=2.,
                 mlp_head_units:List[int]=[2048,1024],
                 num_classes:int=4,
                 drop_attn:float=.2,
                 drop_rs:float=.2,
                 drop_linear:float=.4,
                 trainable_rs:bool=False,
                 original_attn:bool=False,
                 ):
        super(HViT, self).__init__()
        # Parameters
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_heads = num_heads
        self.transformer_layers = transformer_layers
        self.mlp_head_units = mlp_head_units
        self.num_classes = num_classes
        self.drop_attn = drop_attn
        self.drop_rs = drop_rs
        self.drop_linear = drop_linear
        self.trainable_rs = trainable_rs
        self.original_attn = original_attn
        self.num_patches = [(self.img_size//patch)**2 for patch in self.patch_size]
        self.projection_dim = [self.num_channels*patch**2 for patch in self.patch_size]
        self.hidden_units = [int(hidden_unit_factor*self.projection_dim[0]), int(hidden_unit_factor*self.projection_dim[1])]
        # Layers
        self.DPE = DeepPatchEncoder(self.img_size, self.patch_size, self.num_channels)
        if self.original_attn:
            self.TB1 = AttentionTransformerEncoder(self.img_size,self.patch_size[0],self.num_channels,self.num_heads,self.transformer_layers[0], self.hidden_units[0],self.drop_attn)
            self.TB2 = AttentionTransformerEncoder(self.img_size,self.patch_size[1],self.num_channels,self.num_heads,self.transformer_layers[1], self.hidden_units[1],self.drop_attn)
        else:
            self.TB1 = ReAttentionTransformerEncoder(self.img_size,self.patch_size[0],self.num_channels,self.num_heads,self.transformer_layers[0], self.hidden_units[0],self.drop_attn)
            self.TB2 = ReAttentionTransformerEncoder(self.img_size,self.patch_size[1],self.num_channels,self.num_heads,self.transformer_layers[1], self.hidden_units[1],self.drop_attn)
        self.RS = Resampling(self.img_size,self.patch_size,self.num_channels,self.drop_rs, self.trainable_rs)
        self.MLP = tf.keras.Sequential([tf.keras.layers.LayerNormalization(epsilon=1e-6),
                                        tf.keras.layers.Flatten(),
                                        tf.keras.layers.Dropout(self.drop_linear)])
        for i in self.mlp_head_units:
            self.MLP.add(tf.keras.layers.Dense(i))
            self.MLP.add(tf.keras.layers.Dropout(self.drop_linear))
        self.MLP.add(tf.keras.layers.Dense(self.num_classes))



    def call(self, X:tf.Tensor):
        # Patch
        encoded_patches = self.DPE(X)
        # Transformer Block
        encoded_patches = self.TB1(encoded_patches)
        # Resampling
        encoded_patches = self.RS(encoded_patches)
        # Transformer Block
        encoded_patches = self.TB2(encoded_patches)
        # Classify outputs
        logits = self.MLP(encoded_patches)
        return logits