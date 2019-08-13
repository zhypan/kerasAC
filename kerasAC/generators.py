from keras.utils import Sequence
import pandas as pd
import numpy as np
import random
import math
import pysam
from .util import *
import threading
import pickle
import pdb
import tabix

def get_weights(data):
    w1=[float(data.shape[0])/sum(data.iloc[:,i]==1) for i in range(data.shape[1])]
    w0=[float(data.shape[0])/sum(data.iloc[:,i]==0) for i in range(data.shape[1])]
    return w1,w0


def get_probability_thresh_for_precision(truth,predictions,precision_thresh):
    from sklearn.metrics import precision_recall_curve
    num_tasks=truth.shape[1]
    precision_thresholds=[]
    for task_index in range(num_tasks):
        truth_task=truth.iloc[:,task_index]
        pred_task=predictions[:,task_index]
        non_ambig=truth_task!=-1
        precision,recall,threshold=precision_recall_curve(truth_task[non_ambig],pred_task[non_ambig])
        threshold=np.insert(threshold,threshold.shape[0],1)
        merged_prc=pd.DataFrame({'precision':precision,
                                 'recall':recall,
                                 'threshold':threshold})
        precision_thresholds.append(np.min(merged_prc[merged_prc['precision']>=precision_thresh]['threshold']))
    print(precision_thresholds)
    return precision_thresholds

def open_data_file(data_path=None,tasks=None,chroms_to_use=None):
    if data_path.endswith('.hdf5'):
        if tasks==None:
            data=pd.read_hdf(data_path)
        else:
            data=pd.read_hdf(data_path,columns=tasks)
    else:
        #treat as bed file
        if tasks==None:
            data=pd.read_csv(data_path,header=0,sep='\t')
        else:
            data=pd.read_csv(data_path,header=0,sep='\t',nrows=1)
            chrom_col=data.columns[0]
            start_col=data.columns[1]
            end_col=data.columns[2]
            data=pd.read_csv(data_path,header=0,sep='\t',usecols=[chrom_col,start_col,end_col]+tasks)
    print("loaded labels")
    try:
        data=data.set_index(['CHR','START','END'])
        print('set index to CHR, START, END')
    except:
        pass
    if chroms_to_use!=None:
        data=data[np.in1d(data.index.get_level_values(0), chroms_to_use)]
    print("filtered on chroms_to_use")
    print("data.shape:"+str(data.shape))
    return data

class TruePosGenerator(Sequence):
    def __init__(self,data_pickle,ref_fasta,batch_size=128,precision_thresh=0.9,expand_dims=True):
        f=open(data_pickle,'rb')
        data=pickle.load(f)
        self.predictions=data[0]
        self.labels=data[1]
        self.columns=self.labels.columns
        #calculate prediction probability cutoff to achieve the specified precision threshold
        self.prob_thresholds=get_probability_thresh_for_precision(self.labels,self.predictions,precision_thresh)
        truth_pred_product=self.labels*(self.predictions>=self.prob_thresholds)
        true_pos_rows=truth_pred_product[truth_pred_product.max(axis=1)>0]
        self.data=true_pos_rows
        self.indices=np.arange(self.data.shape[0])
        self.add_revcomp=False
        self.ref_fasta=ref_fasta
        self.lock=threading.Lock()
        self.batch_size=batch_size
        self.expand_dims=expand_dims


    def __len__(self):
        return math.ceil(self.data.shape[0]/self.batch_size)

    def __getitem__(self,idx):
        with self.lock:
            self.ref=pysam.FastaFile(self.ref_fasta)
            return self.get_basic_batch(idx)

    def get_basic_batch(self,idx):
        #get seq positions
        inds=self.indices[idx*self.batch_size:(idx+1)*self.batch_size]
        bed_entries=self.data.index[inds]
        #get sequences
        seqs=[self.ref.fetch(i[0],i[1],i[2]) for i in bed_entries]
        #one-hot-encode the fasta sequences
        seqs=np.array([[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in seqs])
        x_batch=seqs
        if (self.expand_dims==True):
            x_batch=np.expand_dims(x_batch,1)
        #extract the labels at the current batch of indices
        y_batch=np.asarray(self.data.iloc[inds])
        return (bed_entries,x_batch,y_batch)


#use wrappers for keras Sequence generator class to allow batch shuffling upon epoch end
class DataGenerator(Sequence):
    def __init__(self,
                 data_path=None,
                 nonzero_bin_path=None,
                 universal_negative_path=None,
                 ref_fasta=None,
                 batch_size=128,
                 add_revcomp=True,
                 tasks=None,
                 shuffled_ref_negatives=False,
                 upsample=True,
                 upsample_ratio=0.1,
                 chroms_to_use=None,
                 get_w1_w0=False,
                 expand_dims=True,
                 vcf_file=None,
                 var_encoding='freq',
                 shuffle=True):

        self.lock = threading.Lock()
        self.shuffle=shuffle
        self.batch_size=batch_size
        #decide if reverse complement should be used
        self.add_revcomp=add_revcomp
        if add_revcomp==True:
            self.batch_size=int(batch_size/2)

        #determine whether negative set should consist of the shuffled refs.
        # If so, split batch size in 2, as each batch will be augmented with shuffled ref negatives
        # in ratio equal to positives
        self.shuffled_ref_negatives=shuffled_ref_negatives
        if self.shuffled_ref_negatives==True:
            self.batch_size=int(self.batch_size/2)

        #open the reference file
        self.ref_fasta=ref_fasta

        self.data_path=data_path
        self.nonzero_bin_path=nonzero_bin_path
        self.universal_negative_path=universal_negative_path
        if self.data_path is not None:
            self.data=open_data_file(data_path=data_path,tasks=tasks,chroms_to_use=chroms_to_use)
            self.indices=np.arange(self.data.shape[0])
        else:
            self.nonzero_bins=open_data_file(data_path=nonzero_bin_path,tasks=tasks,chroms_to_use=chroms_to_use)
            self.universal_negatives=open_data_file(data_path=universal_negative_path,chroms_to_use=chroms_to_use)
            self.indices=np.arange(self.nonzero_bins.shape[0]+self.universal_negatives.shape[0])
            self.universal_negative_offset=self.nonzero_bins.shape[0]

        num_indices=self.indices.shape[0]
        if get_w1_w0==True:
            assert self.data is not None
            w1,w0=get_weights(self.data)
            self.w1=w1
            self.w0=w0
            #print(self.w1)

        self.add_revcomp=add_revcomp
        self.expand_dims=expand_dims


        #set variables needed for upsampling the positives
        self.upsample=upsample
        if self.upsample==True:
            self.upsample_ratio=upsample_ratio
            if self.data_path is not None:
                self.ones = self.data.loc[(self.data > 0).any(axis=1)]
                #extract the indices where all bins are 0
                self.zeros = self.data.loc[(self.data < 1).all(axis=1)].index
            else:
                #use the provided sets of nonzero bins and universal negatives
                self.ones=self.nonzero_bins
                self.zeros=self.universal_negatives

            self.pos_batch_size = int(self.batch_size * self.upsample_ratio)
            self.neg_batch_size = self.batch_size - self.pos_batch_size
            self.pos_indices=np.arange(self.ones.shape[0])
            self.neg_indices=np.arange(self.zeros.shape[0])

            #wrap the positive and negative indices to reach size of self.indices
            num_pos_wraps=math.ceil(num_indices/self.pos_indices.shape[0])
            num_neg_wraps=math.ceil(num_indices/self.neg_indices.shape[0])
            self.pos_indices=np.repeat(self.pos_indices,num_pos_wraps)[0:num_indices]
            self.neg_indices=np.repeat(self.neg_indices,num_neg_wraps)[0:num_indices]
            if self.shuffle==True:
                np.random.shuffle(self.pos_indices)
                np.random.shuffle(self.neg_indices)

        if vcf_file != None:
            self.vcf = vcf_file
        self.var_encoding = var_encoding


    def __len__(self):
        return math.ceil(len(self.indices)/self.batch_size)

    def __getitem__(self,idx):
        with self.lock:
            ref=pysam.FastaFile(self.ref_fasta)
            self.ref=ref
            if self.shuffled_ref_negatives==True:
                return self.get_shuffled_ref_negatives_batch(idx)
            elif self.upsample==True:
                return self.get_upsampled_positives_batch(idx)
            else:
                return self.get_basic_batch(idx)

    def get_shuffled_ref_negatives_batch(self,idx):
        #get seq positions
        inds=self.indices[idx*self.batch_size:(idx+1)*self.batch_size]
        bed_entries=self.data.index[inds]
        #get sequences
        seqs=[self.ref.fetch(i[0],i[1],i[2]) for i in bed_entries]
        if self.add_revcomp==True:
            #add in the reverse-complemented sequences for training.
            seqs_rc=[revcomp(s) for s in seqs]
            seqs=seqs+seqs_rc

        #generate the corresponding negative set by dinucleotide-shuffling the sequences
        seqs_shuffled=[dinuc_shuffle(s) for s in seqs]
        seqs=seqs+seqs_shuffled
        #one-hot-encode the fasta sequences
        seqs=np.array([[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in seqs])
        x_batch=seqs
        if (self.expand_dims==True):
            x_batch=np.expand_dims(x_batch,1)
        y_batch=np.asarray(self.data.iloc[inds])
        if self.add_revcomp==True:
            y_batch=np.concatenate((y_batch,y_batch),axis=0)
        y_shape=y_batch.shape
        y_batch=np.concatenate((y_batch,np.zeros(y_shape)))
        return (x_batch,y_batch)

    def get_upsampled_positives_batch(self,idx):
        #get seq positions
        pos_inds=self.pos_indices[idx*self.pos_batch_size:(idx+1)*self.pos_batch_size]
        pos_bed_entries=self.ones.index[pos_inds]
        neg_inds=self.neg_indices[idx*self.neg_batch_size:(idx+1)*self.neg_batch_size]
        neg_bed_entries=self.zeros.index[neg_inds]

        #print(neg_inds[0:10])
        #bed_entries=pos_bed_entries+neg_bed_entries

        #get sequences
        pos_seqs=[self.ref.fetch(i[0],i[1],i[2]) for i in pos_bed_entries]
        neg_seqs=[self.ref.fetch(i[0],i[1],i[2]) for i in neg_bed_entries]

        base_to_index = {'a':0,
                         'c':1,
                         'g':2,
                         't':3,
                         'A':0,
                         'C':1,
                         'G':2,
                         'T':3}

        if self.vcf != None and self.var_encoding == 'personal':
            vcf_ = tabix.open(self.vcf)
            for seq_index in range(len(pos_bed_entries)):
                bed_entry=pos_bed_entries[seq_index]
                cur_start=bed_entry[1]
                cur_end=bed_entry[2]
                max_offset=cur_end-cur_start
                variants=[i for i in vcf_.query(bed_entry[0],bed_entry[1],bed_entry[2])]
                if variants != []:
                    for variant in variants:
                        if len(variant[3]) == len(variant[4]) and len(variant[4]) == 1 and variant[9].split(':')[0] == '1/1':
                            pos=int(variant[1])
                            offset=int(pos-1-cur_start)
                            assert offset <= max_offset
                            pos_seqs[seq_index] = pos_seqs[seq_index][:offset] + variant[4] + pos_seqs[seq_index][offset+1:]
            for seq_index in range(len(neg_bed_entries)):
                bed_entry=neg_bed_entries[seq_index]
                cur_start=bed_entry[1]
                cur_end=bed_entry[2]
                max_offset=cur_end-cur_start
                variants=[i for i in vcf_.query(bed_entry[0],bed_entry[1],bed_entry[2])]
                if variants != []:
                    for variant in variants:
                        if len(variant[3]) == len(variant[4]) and len(variant[4]) == 1 and variant[9].split(':')[0] == '1/1':
                            pos=int(variant[1])
                            offset=int(pos-1-cur_start)
                            assert offset <= max_offset
                            neg_seqs[seq_index] = neg_seqs[seq_index][:offset] + variant[4] + neg_seqs[seq_index][offset+1:]


        if self.vcf != None and self.var_encoding == 'freq':
            vcf_ = tabix.open(self.vcf)
            if self.add_revcomp == True:
                pos_seqs_rc = [revcomp(s) for s in pos_seqs]
                neg_seqs_rc = [revcomp(s) for s in neg_seqs]
                pos_seqs_rc = [[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in pos_seqs_rc]
                neg_seqs_rc = [[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in neg_seqs_rc]
            pos_seqs = [[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in pos_seqs]
            neg_seqs = [[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in neg_seqs]
            for seq_index in range(len(pos_bed_entries)):
                bed_entry=pos_bed_entries[seq_index]
                cur_start=bed_entry[1]
                cur_end=bed_entry[2]
                max_offset=cur_end-cur_start
                variants=[i for i in vcf_.query(bed_entry[0],bed_entry[1],bed_entry[2])]
                if variants != []:
                    for variant in variants:
                        if len(variant[3]) == len(variant[4]) and len(variant[4]) == 1:
                            pos=int(variant[1])
                            offset=int(pos-1-cur_start)
                            assert offset <= max_offset
                            assert pos_seqs[seq_index][offset][base_to_index[variant[3]]] == 1
                            pos_seqs[seq_index][offset][base_to_index[variant[3]]] = 0.5
                            pos_seqs[seq_index][offset][base_to_index[variant[4]]] = 0.5
                            if self.add_revcomp == True:
                                pos_seqs_rc[seq_index][len(pos_bed_entries[seq_index])-1-offset][base_to_index[revcomp(variant[3])]] = 0.5
                                pos_seqs_rc[seq_index][len(pos_bed_entries[seq_index])-1-offset][base_to_index[revcomp(variant[4])]] = 0.5
            for seq_index in range(len(neg_bed_entries)):
                bed_entry=neg_bed_entries[seq_index]
                cur_start=bed_entry[1]
                cur_end=bed_entry[2]
                max_offset=cur_end-cur_start
                variants=[i for i in vcf_.query(bed_entry[0],bed_entry[1],bed_entry[2])]
                if variants != []:
                    for variant in variants:
                        if len(variant[3]) == len(variant[4]) and len(variant[4]) == 1:
                            pos=int(variant[1])
                            offset=int(pos-1-cur_start)
                            assert offset <= max_offset
                            assert neg_seqs[seq_index][offset][base_to_index[variant[3]]] == 1
                            neg_seqs[seq_index][offset][base_to_index[variant[3]]] = 0.5
                            neg_seqs[seq_index][offset][base_to_index[variant[4]]] = 0.5
                            if self.add_revcomp == True:
                                neg_seqs_rc[seq_index][len(neg_bed_entries[seq_index])-1-offset][base_to_index[revcomp(variant[3])]] = 0.5
                                neg_seqs_rc[seq_index][len(neg_bed_entries[seq_index])-1-offset][base_to_index[revcomp(variant[4])]] = 0.5
            seqs = pos_seqs + neg_seqs
            if self.add_revcomp == True:
                seqs_rc = pos_seqs_rc + neg_seqs_rc
                seqs = seqs + seqs_rc
            seqs = np.array(seqs)

        else:
            seqs=pos_seqs+neg_seqs
            if self.add_revcomp==True:
                #add in the reverse-complemented sequences for training.
                seqs_rc=[revcomp(s) for s in seqs]
                seqs=seqs+seqs_rc
            #one-hot-encode the fasta sequences
            seqs=np.array([[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in seqs])

        x_batch=seqs
        if (self.expand_dims==True):
            x_batch=np.expand_dims(x_batch,1)

        #extract the positive and negative labels at the current batch of indices
        y_batch_pos=self.ones.iloc[pos_inds]
        y_batch_neg=np.zeros((x_batch.shape[0]-y_batch_pos.shape[0],y_batch_pos.shape[1]))
        y_batch=np.concatenate((y_batch_pos,y_batch_neg),axis=0)
        #add in the labels for the reverse complement sequences, if used
        if self.add_revcomp==True:
            y_batch=np.concatenate((y_batch,y_batch),axis=0)
        #pdb.set_trace()
        #print(y_batch.shape)
        #print(x_batch.shape)
        return (x_batch,y_batch)

    def get_basic_batch(self,idx):
        #get seq positions
        inds=self.indices[idx*self.batch_size:(idx+1)*self.batch_size]
        if self.data_path is not None:
            bed_entries=self.data.index[inds]
        else:
            pos_inds=inds[inds<self.universal_negative_offset]
            neg_inds=inds[inds>=self.universal_negative_offset]
            bed_entries=np.concatenate((self.nonzero_bins.index[pos_inds],self.universal_negatives.index[neg_inds-self.universal_negative_offset]),axis=0)
        #get sequences
        seqs=[self.ref.fetch(i[0],i[1],i[2]) for i in bed_entries]

        if self.vcf != None and self.var_encoding == 'personal':
            vcf_ = tabix.open(self.vcf)
            for seq_index in range(len(bed_entries)):
                bed_entry=bed_entries[seq_index]
                cur_start=bed_entry[1]
                cur_end=bed_entry[2]
                max_offset=cur_end-cur_start
                variants=[i for i in vcf_.query(bed_entry[0],bed_entry[1],bed_entry[2])]
                if variants != []:
                    for variant in variants:
                        if len(variant[3]) == len(variant[4]) and len(variant[4]) == 1 and variant[9].split(':')[0] == '1/1':
                            pos=int(variant[1])
                            offset=int(pos-1-cur_start)
                            assert offset <= max_offset
                            seqs[seq_index] = seqs[seq_index][:offset] + variant[4] + seqs[seq_index][offset+1:]

        if self.add_revcomp==True:
            #add in the reverse-complemented sequences for training.
            seqs_rc=[revcomp(s) for s in seqs]
            seqs=seqs+seqs_rc
        #one-hot-encode the fasta sequences
        seqs=np.array([[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in seqs])
        x_batch=seqs
        if(self.expand_dims==True):
            x_batch=np.expand_dims(x_batch,1)
        #extract the labels at the current batch of indices
        if self.data_path is not None:
            y_batch=np.asarray(self.data.iloc[inds])
        else:
            y_batch_pos=self.nonzero_bins.iloc[pos_inds]
            y_batch_neg=np.zeros((x_batch.shape[0]-y_batch_pos.shape[0],y_batch_pos.shape[1]))
            y_batch=np.concatenate((y_batch_pos,y_batch_neg),axis=0)
        #add in the labels for the reverse complement sequences, if used
        if self.add_revcomp==True:
            y_batch=np.concatenate((y_batch,y_batch),axis=0)
        return (x_batch,y_batch)

    def on_epoch_end(self):
        #if upsampling is being used, shuffle the positive and negative indices
        if self.shuffle==True:
            if self.upsample==True:
                np.random.shuffle(self.pos_indices)
                np.random.shuffle(self.neg_indices)
            else:
                np.random.shuffle(self.indices)
    def get_labels(self):
        if self.data_path is not None:
            return self.data
        else:
            labels_nonzero=self.nonzero_bins
            labels_universal_negatives=self.universal_negatives
            for task in labels_nonzero.columns:
                if task in ['CHROM','START','END']:
                    continue
                labels_universal_negatives[task]=np.zeros(labels_universal_negatives.shape[0])
            labels=pd.concat([labels_nonzero,labels_universal_negatives])
            return labels

#generate batches of SNP data with specified allele column name and flank size
class SNPGenerator(DataGenerator):
    def __init__(self,data_path,ref_fasta,allele_col,flank_size=500,batch_size=128,expand_dims=True,add_revcomp=False):
        DataGenerator.__init__(self,data_path=data_path,ref_fasta=ref_fasta,batch_size=batch_size,add_revcomp=add_revcomp,upsample=False,expand_dims=expand_dims)
        self.allele_col=allele_col
        self.flank_size=flank_size

    #override the get_basic_batch definition to extract flanks around the bQTL
    # and insert the specified allele
    def get_basic_batch(self,idx):
        #get seq positions
        inds=self.indices[idx*self.batch_size:(idx+1)*self.batch_size]
        entries=self.data.iloc[inds]
        seqs=[]
        for index,row in entries.iterrows():
            allele=row[self.allele_col].split(',')[0]
            chrom=index[0]
            pos=index[1]-1
            start=pos-self.flank_size
            end=pos+self.flank_size
            seq=self.ref.fetch(chrom,start,end)
            left=seq[0:self.flank_size]
            right=seq[self.flank_size+len(allele)::]
            seq=seq[0:self.flank_size]+allele+seq[self.flank_size+len(allele)::]
            seqs.append(seq)
        #one-hot-encode the fasta sequences
        seqs=np.array([[ltrdict.get(x,[0,0,0,0]) for x in seq] for seq in seqs])
        x_batch=seqs
        if (self.expand_dims==True):
            x_batch=np.expand_dims(x_batch,1)
        return x_batch

