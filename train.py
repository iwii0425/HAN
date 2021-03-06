#! ~/usr/bin/env python
import tensorflow as tf
from tensorflow import flags
from data import generate
import h5py as h5
import numpy as np
import time
#reproduction of HAN 

tf.logging.set_verbosity(tf.logging.INFO)


FLAGS = flags.FLAGS
flags.DEFINE_string("data_dir",'data/yelp-2013', 'directory containing train, val and test h5')
flags.DEFINE_string('checkpoint_dir','checkpoint','directory to save the best model saved as checkpoint_dir/model.chkpt')

flags.DEFINE_string('restore_checkpoint',None,'restore to a state; if None train from scratch default : ')

flags.DEFINE_boolean('gpu',True,'use --nogpu to disable gpu')
flags.DEFINE_integer('epoch',2,'epoch : default 2s')
flags.DEFINE_integer('batchsize',32,'batchsize: default 32') 


#network parameters
flags.DEFINE_float('hidden_dim',100 ,'GRU hidden dimension : default 100')

#hyper params
flags.DEFINE_float('lr',1e-3,'Learning rate : default 1e-3')


class Attention():
    def __init__(self, input, mask, scope ='A0'):
        assert input.get_shape().as_list()[:-1] == mask.get_shape().as_list() and len(mask.get_shape().as_list()) == 2
        _, steps, embed_dim = input.get_shape().as_list()
        print steps, embed_dim
        #trainable variales
        self.u_w = tf.Variable(tf.truncated_normal([1, embed_dim], stddev=0.1),  name='%s_query' %scope, dtype=tf.float32)
        weights = tf.Variable(tf.truncated_normal([embed_dim, embed_dim], stddev=0.1),  name='%s_Weight' %scope, dtype=tf.float32)
        bias = tf.Variable(tf.truncated_normal([1, embed_dim], stddev=0.1),  name='%s_bias' %scope, dtype=tf.float32)
        #equations
        u_i = tf.tanh(tf.matmul(tf.reshape(input,[-1,embed_dim]), weights) + bias)
        u_i = tf.reshape(u_i, [-1,steps, embed_dim])
        distances = tf.reduce_sum(tf.mul(u_i, self.u_w), reduction_indices=-1)
        self.debug = distances
        self.distances = distances -tf.expand_dims(tf.reduce_max(distances),-1) #avoid exp overflow
        
        expdistance = tf.mul(tf.exp(self.distances), mask) #
        Denom = tf.expand_dims(tf.reduce_sum(expdistance, reduction_indices=1), 1) + 1e-13 #avoid 0/0 error
        self.Attn = expdistance/Denom
        print 'Attn', self.Attn.get_shape()
        return


class HAN():
    def __init__(self, x, mask, **kwargs):
        
        assert x.get_shape().as_list()[1:-1] == mask.get_shape().as_list()[1:]
        _, doclen, sentlen, embed_dim = x.get_shape().as_list()
        with tf.device(kwargs.get('device','/cpu:0')):
            xnew = tf.reshape(x,[-1, sentlen, embed_dim]) #example_sentences, steps, embedding
            masknew = tf.reshape(mask, [-1,sentlen]) #wordmask
            xnew = tf.unpack(xnew, axis=1)
    
            cell_fw = tf.nn.rnn_cell.GRUCell(kwargs.get('hidden_dim',100))
            cell_bw = tf.nn.rnn_cell.GRUCell(kwargs.get('hidden_dim',100))
            output,_,_ = tf.nn.bidirectional_rnn(cell_fw, cell_bw, xnew, dtype=tf.float32, scope='L0')
    
            output = tf.pack(output, axis=1)
            self.A0 = Attention(output, masknew, scope='A0')

            sentence_emb = tf.reduce_sum(output*tf.expand_dims(self.A0.Attn,-1) , reduction_indices=1) #sum_j Attn[i][j]*Word_embed[i][j][:]
            sentence_emb = tf.reshape(sentence_emb, [-1, doclen, 2*kwargs.get('hidden_dim',100)])
            print 'sentence_emb' ,sentence_emb.get_shape()
            masknew = tf.cast(tf.reduce_sum(mask, reduction_indices= -1)>0,tf.float32) #sentence mask
            output = tf.unpack(sentence_emb, axis=1)
            cell_fw = tf.nn.rnn_cell.GRUCell(kwargs.get('hidden_dim',100))
            cell_bw = tf.nn.rnn_cell.GRUCell(kwargs.get('hidden_dim',100))
            output,_,_ = tf.nn.bidirectional_rnn(cell_fw, cell_bw, output, dtype = tf.float32, scope='L1')
            output = tf.pack(output, axis=1)

            self.A1 = Attention(output, masknew, scope='A1')        
            self.output = tf.reduce_sum(sentence_emb*tf.expand_dims(self.A1.Attn,-1) , reduction_indices=1) #sum_j Attn[i][j]*Senten_embed[i][j][:]
            print 'doc_embe' , self.output.get_shape()
        return 
        
if FLAGS.gpu:
    device = '/gpu:0'
else:
    device = '/cpu:0'
################## STANDARD
D = h5.File('%s/train.h5' %FLAGS.data_dir)
pretrained_embedding_matrix = np.load('%s/embed.npy' %FLAGS.data_dir)
#############################
NUM_CLASSES = D['y'].shape[1]
pre_output_size = 256
INPUT_SHAPE = [None,]+ list(D['x'].shape[1:])
OUTPUT_SHAPE = [None, D['y'].shape[1]]
WE_SHAPE = pretrained_embedding_matrix.shape
embed_dim =  WE_SHAPE[1]
with tf.device('/cpu:0'):
    x  = tf.placeholder(tf.int32, INPUT_SHAPE, name='documentVector')
    y  = tf.placeholder(tf.int32, OUTPUT_SHAPE, name='output')
    mask = tf.placeholder(tf.float32, INPUT_SHAPE, name = 'document_Mask')
    pretrained_we = tf.placeholder(tf.float32, WE_SHAPE, name='WordEmbedding_Pretrained')

    WELayer = tf.Variable(tf.truncated_normal(WE_SHAPE), dtype=tf.float32)
    embedding_init = WELayer.assign(pretrained_we)

    _, doclen,sentlen =  x.get_shape().as_list()
    xnew = tf.reshape(x, [-1,sentlen])
    WE = tf.nn.embedding_lookup(WELayer, xnew)
############Document Model#################
H = HAN(tf.reshape(WE, [-1, doclen, sentlen, embed_dim]), mask, device=device)

#######Classifier################
with tf.device(device):
    output = tf.contrib.layers.fully_connected(H.output, pre_output_size, activation_fn = tf.tanh, scope='fc0')
    output = tf.contrib.layers.fully_connected(output ,NUM_CLASSES, scope='fc1', activation_fn=None)


    assert y.get_shape().as_list() == output.get_shape().as_list()

    ########Loss##################
    log_softmax_output = tf.log(tf.nn.softmax(output)+1e-13) #log softmax 1e-13 for stability
    loss = -NUM_CLASSES*tf.reduce_mean(tf.mul(log_softmax_output, tf.cast(y,tf.float32))) #log aka cross entropy (close) aka logistic loss
    global_step = tf.Variable(0, trainable=False)
    train_op = tf.contrib.layers.optimize_loss(loss, global_step, learning_rate=FLAGS.lr, optimizer='Adam')

#Metrics
acc  = tf.reduce_mean(tf.cast(tf.equal(tf.argmax(output, 1) ,tf.argmax(y, 1)), tf.float32))
init_op = tf.initialize_all_variables()

sess = tf.Session(config=tf.ConfigProto(log_device_placement=True, allow_soft_placement = True))
sess.run(init_op)
sess.run(embedding_init, feed_dict = {pretrained_we:pretrained_embedding_matrix})

saver = tf.train.Saver()
if FLAGS.restore_checkpoint:
    saver.restore(sess, '%s' %FLAGS.restore_checkpoint )
    print('Restoring Model... ')
start, best_val = time.time(),-1
for batch_x, batch_y, batch_mask in generate('%s/train.h5' %FLAGS.data_dir, FLAGS.epoch, FLAGS.batchsize):
    l,a,g,_ = sess.run([loss, acc, global_step, train_op], feed_dict = {x: batch_x, y: batch_y, mask: batch_mask})
    print 'Train Iteration %d: Loss %.3f acc: %.3f ' %(g,l,a)
    if g%150000 == 0: #0.5 epoch
        print ('Time taken for %d  iterations %.3f' %(g,time.time()-start))
        avg_loss, avg_acc, examples = 0.0, 0.0, 0.0
        for val_x, val_y, val_mask in generate('%s/dev.h5' %FLAGS.data_dir,1, 32):
            l, a = sess.run([loss, acc], feed_dict = {x:val_x, y:val_y, mask: val_mask})
            avg_loss +=l*val_y.shape[0]
            avg_acc +=a*val_y.shape[0]
            examples += val_y.shape[0]
            print examples, avg_loss*1./examples, avg_acc*1./examples
        print('Val loss %.3f accuracy %.3f' %(avg_loss*1./examples, avg_acc*1./examples))
        val = avg_acc*1./examples
        if best_val < val:          
            best_val = val
            save_path = saver.save(sess, "%s/model.ckpt" %FLAGS.checkpoint_dir)
            print('Model Saved @ %s' %save_path)

        print('Best val accuracy %.3f' %best_val)

sess.close()
