from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import time
#graceful shutdown
import psutil
import signal 
import os

#multithreading
#from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import Pool,Process, Queue 

import warnings
import numpy as np
import pysam
import pandas as pd

import tensorflow as tf 
from kerasAC.activations import softMaxAxis1
from .generators.basic_generator import *
#from .generators.tiledb_predict_generator import * 
from kerasAC.config import args_object_from_args_dict
from kerasAC.performance_metrics import *
from kerasAC.custom_losses import *
from kerasAC.metrics import recall, specificity, fpr, fnr, precision, f1
import argparse
import yaml 
import h5py 
import pickle
import numpy as np 
import keras 
from keras.losses import *
from keras.models import Model 
from kerasAC.custom_losses import *
from abstention.calibration import PlattScaling, IsotonicRegression 
import random
import pdb 

def write_predictions(args):
    try:
        out_predictions=args.predictions_hdf5+".predictions" 
        first=True
        while True:
            pred_df=pred_queue.get()
            if type(pred_df) == str: 
                if pred_df=="FINISHED":
                    return
            if first is True:
                mode='w'
                first=False
                append=False
            else:
                mode='a'
                append=True
            pred_df.to_hdf(out_predictions,key="data",mode=mode, append=append,format="table", min_itemsize={'index':30})
    except KeyboardInterrupt:
        #shutdown the pool
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise 
    except Exception as e:
        print(e)
        #shutdown the pool
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise e

def write_labels(args):
    try:
        out_labels=args.predictions_hdf5+".labels" 
        first=True
        while True:
            label_df=label_queue.get()
            if type(label_df)==str:
                if label_df=="FINISHED":
                    return
            if first is True:
                mode='w'
                first=False
                append=False
            else:
                mode='a'
                append=True
            label_df.to_hdf(out_labels,key="data",mode=mode, append=append,format="table", min_itemsize={'index':30})
    except KeyboardInterrupt:
        #shutdown the pool
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise 
    except Exception as e:
        print(e)
        #shutdown the pool
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise e

def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)

def kill_child_processes(parent_pid, sig=signal.SIGTERM):
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return
    children = parent.children(recursive=True)
    for process in children:
        process.send_signal(sig)
        
def get_weights(args):
    w1=None
    w0=None
    if args.w1_w0_file!=None:
        w1_w0=np.loadtxt(args.w1_w0_file)
        w1=w1_w0[:,0]
        w0=w1_w0[:,1]
    if args.w1!=None:
        w1=args.w1
    if args.w0!=None:
        w0=args.w0 
    return w1,w0


def get_batch_wrapper(idx):
    X,y,x_pos,y_pos=test_generator[idx]
    y=y.squeeze(axis=-1)
    #represent coords w/ string, MultiIndex not supported in table append mode
    y_pos=pd.MultiIndex.from_tuples(y_pos)
    y=pd.DataFrame(y,index=y_pos)
    return [X,y,y_pos,idx]


def get_tiledb_predict_generator(args):
    global test_generator
    test_generator=TiledbPredictGenerator(index_path=args.index_path,
                                          input_path=args.input_path,
                                          output_path=args.output_path,
                                          batch_size=args.batch_size,
                                          task_file=args.tiledb_tasks_file,
                                          ref_fasta=args.ref_fasta,
                                          label_source=args.label_source_attribute,
                                          label_flank=args.label_flank,
                                          label_aggregation=args.label_aggregation,
                                          label_subset_attribute=args.label_subset_attribute,
                                          label_thresh=args.label_thresh,
                                          sequence_flank=args.sequence_flank,
                                          tiledb_stride=args.tiledb_stride,
                                          chrom_sizes_file=args.chrom_sizes,
                                          chroms=args.predict_chroms)
    print("created TiledbPredictGenerator")    
    return test_generator 
def get_hdf5_predict_generator(args):
    test_generator=DataGenerator(index_path=args.index_data_path,
                                 input_path=args.input_data_path,
                                 output_path=args.output_data_path,
                                 index_tasks=args.index_tasks,
                                 num_inputs=args.num_inputs,
                                 num_outputs=args.num_outputs,
                                 ref_fasta=args.ref_fasta,
                                 batch_size=args.batch_size,
                                 add_revcomp=False,
                                 chroms_to_use=args.predict_chroms,
                                 expand_dims=args.expand_dims,
                                 tasks=args.tasks,
                                 shuffle=False)
    return test_generator

def predict_on_batch_wrapper(args,model,test_generator):
    num_batches=len(test_generator)
    processed=0
    try:
        with Pool(processes=args.threads,initializer=init_worker) as pool: 
            while (processed < num_batches):
                idset=range(processed,min([num_batches,processed+args.max_queue_size]))
                for result in pool.imap_unordered(get_batch_wrapper,idset):
                    X=result[0]
                    y=result[1]
                    y_pos=result[2]
                    idx=result[3]
                    processed+=1
                    if processed%10==0:
                        print(str(processed)+"/"+str(num_batches))
                    #get the model predictions            
                    preds=model.predict_on_batch(X)
                    preds=preds.squeeze(axis=-1)
                    label_queue.put(y)
                    pred_queue.put(pd.DataFrame(preds,index=y_pos))
                    
    except KeyboardInterrupt:
        #shutdown the pool
        pool.terminate()
        pool.join() 
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise 
    except Exception as e:
        print(e)
        #shutdown the pool
        pool.terminate()
        pool.join()
        # Kill remaining child processes
        kill_child_processes(os.getpid())
        raise e
    print("finished with tiledb predictions!")
    label_queue.put("FINISHED")
    label_queue.close() 
    pred_queue.put("FINISHED")
    pred_queue.close() 
    return

def get_predictions_bed(args,model):
    test_generator=DataGenerator(index_path=args.index_data_path,
                                 input_path=args.input_data_path,
                                 output_path=args.output_data_path,
                                 index_tasks=args.index_tasks,
                                 num_inputs=args.num_inputs,
                                 num_outputs=args.num_outputs,
                                 ref_fasta=args.ref_fasta,
                                 batch_size=args.batch_size,
                                 add_revcomp=False,
                                 chroms_to_use=args.predict_chroms,
                                 expand_dims=args.expand_dims,
                                 tasks=args.tasks,
                                 shuffle=False)

    predictions=model.predict_generator(test_generator,
                                  max_queue_size=args.max_queue_size,
                                  workers=args.threads,
                                  use_multiprocessing=True,
                                  verbose=1)
    print("got predictions")
    perform_calibration=args.calibrate_classification or args.calibrate_regression
    preacts=None
    if perform_calibration==True:
        if args.calibrate_classification==True:
            print("getting logits")
            preact_model=Model(inputs=model.input,
                               outputs=model.layers[-2].output)
        elif args.calibrate_regression==True:
            print("getting pre-relu outputs (preacts)")
            preact_model=Model(inputs=model.input,
                              outputs=model.layers[-1].output) 
        test_generator=DataGenerator(index_path=args.index_data_path,
                                     input_path=args.input_data_path,
                                     output_path=args.output_data_path,
                                     index_tasks=args.index_data_path,
                                     num_inputs=args.num_inputs,
                                     num_outputs=args.num_outputs,
                                     ref_fasta=args.ref_fasta,
                                     add_revcomp=False,
                                     batch_size=args.batch_size,
                                     chroms_to_use=args.predict_chroms,
                                     expand_dims=args.expand_dims,
                                     tasks=args.tasks,
                                     shuffle=False)
        
        preacts=preact_model.predict_generator(test_generator,
                                  max_queue_size=args.max_queue_size,
                                  workers=args.threads,
                                  use_multiprocessing=True,
                                  verbose=1)
        print("preactivation shape:"+str(preacts.shape))
    labels=test_generator.get_labels()
    outputs={'predictions':predictions,
             'labels':labels,
             'preacts':preacts}
    return outputs

def get_predictions_variant(args,model):
    test_generator=SNPGenerator(args.ref_col,
                                args.flank,
                                index_path=args.index_data_path,
                                input_path=args.input_data_path,
                                output_path=args.output_data_path,
                                index_tasks=args.index_tasks,
                                num_inputs=args.num_inputs,
                                num_outputs=args.num_outputs,
                                ref_fasta=args.ref_fasta,
                                allele_col=args.ref_col,
                                batch_size=args.batch_size,
                                add_revcomp=False,
                                chroms_to_use=args.predict_chroms,
                                expand_dims=args.expand_dims,
                                tasks=args.tasks,
                                shuffle=False)
    ref_predictions=model.predict_generator(test_generator,
                                  max_queue_size=args.max_queue_size,
                                  workers=args.threads,
                                  use_multiprocessing=True,
                                  verbose=1)
    test_generator.allele_col=args.alt_col
    alt_predictions=model.predict_generator(test_generator,
                                  max_queue_size=args.max_queue_size,
                                  workers=args.threads,
                                  use_multiprocessing=True,
                                  verbose=1)
    
    print("got predictions")
    labels=test_generator.get_batch_coords_and_labels()
    print("got labels")
    outputs={'ref_predictions':ref_predictions,
             'alt_predictions':alt_prefictions,
             'ref_labels':labels}
    
    perform_calibration=args.calibrate_classification or args.calibrate_regression
    preacts=None
    if perform_calibration==True:
        if args.calibrate_classification==True:
            print("getting logits")
            preact_model=Model(inputs=model.input,
                               outputs=model.layers[-2].output)
        elif args.calibrate_regression==True:
            print("getting pre-relu outputs (preacts)")
            preact_model=Model(inputs=model.input,
                              outputs=model.layers[-1].output) 
        test_generator.allele_col=args.ref_col
        ref_preacts=preact_model.predict_generator(test_generator,
                                  max_queue_size=args.max_queue_size,
                                  workers=args.threads,
                                  use_multiprocessing=True,
                                  verbose=1)
        test_generator.allele_col=args.alt_col
        alt_preacts=preact_model.predict_generator(test_generator,
                                  max_queue_size=args.max_queue_size,
                                  workers=args.threads,
                                  use_multiprocessing=True,
                                  verbose=1)
        
        print("preactivation shape:"+str(preacts.shape))
        outputs['ref_preacts']=ref_preacts
        outputs['alt_preacts']=alt_preacts
    return outputs


def parse_args():
    parser=argparse.ArgumentParser(description='Provide model files  & a dataset, get model predictions')
    
    input_data_path=parser.add_argument_group("input_data_path")
    input_data_path=parser.add_argument_group('input_data_path')
    input_data_path.add_argument("--index_data_path",default=None,help="seqdataloader output hdf5, or tsv file containing binned labels")
    input_data_path.add_argument("--index_tasks",nargs="*",default=None)    
    input_data_path.add_argument("--input_data_path",nargs="+",default=None,help="seq or path to seqdataloader hdf5")
    input_data_path.add_argument("--output_data_path",nargs="+",default=None,help="path to seqdataloader hdf5")
    
    input_data_path.add_argument('--variant_bed',default=None)
    input_data_path.add_argument('--predictions_pickle_to_load',help="if predictions have already been generated, provide a pickle with them to just compute the accuracy metrics",default=None)
    input_data_path.add_argument('--ref_fasta')

    tiledbgroup=parser.add_argument_group("tiledb")
    tiledbgroup.add_argument("--tdb_outputs")
    tiledbgroup.add_argument("--tdb_output_source_attribute",nargs="+",default="fc_bigwig",help="tiledb attribute for use in label generation i.e. fc_bigwig")
    tiledbgroup.add_argument("--tdb_output_flank",nargs="+",type=int,help="flank around bin center to use in generating outputs")
    tiledbgroup.add_argument("--tdb_output_aggregation",nargs="+",default=None,help="method for output aggreagtion; one of None, 'avg','max'")
    tiledbgroup.add_argument("--tdb_output_transformation",nargs="+",default=None,help="method for output transformation; one of None, 'log','log10','asinh'")
    
    tiledbgroup.add_argument("--tdb_inputs",nargs="+")
    tiledbgroup.add_argument("--tdb_input_source_attribute",nargs="+",type=int,help="attribute to use for generating model input, or 'seq' for one-hot-encoded sequence")
    tiledbgroup.add_argument("--tdb_input_flank",nargs="+",type=int,help="length of sequence around bin center to use for input")
    tiledbgroup.add_argument("--tdb_input_aggregation",nargs="+",default=None,help="method for input aggregation; one of 'None','avg','max'")
    tiledbgroup.add_argument("--tdb_input_transformation",nargs="+",default=None,help="method for input transformation; one of None, 'log','log10','asinh'")

    tiledbgroup.add_argument("--tdb_indexer",default=None,help="tiledb paths for each input task",action="append")
    tiledbgroup.add_argument("--tdb_partition_attribute_for_upsample",default="idr_peak",help="tiledb attribute to use for upsampling, i.e. idr_peak")
    tiledbgroup.add_argument("--tdb_partition_thresh_for_upsample",type=float,default=1,help="values >= partition_thresh_for_upsample within the partition_attribute_for_upsample will be upsampled during training")
    tiledbgroup.add_argument("--chrom_sizes",default=None,help="chromsizes file for use with tiledb generator")
    tiledbgroup.add_argument("--tiledb_stride",type=int,default=1)

    input_filtering_params=parser.add_argument_group("input_filtering_params")    
    input_filtering_params.add_argument('--predict_chroms',nargs="*",default=None)
    input_filtering_params.add_argument('--center_on_summit',default=False,action='store_true',help="if this is set to true, the peak will be centered at the summit (must be last entry in bed file or hammock) and expanded args.flank to the left and right")
    input_filtering_params.add_argument("--tasks",nargs="*",default=None)
    
    output_params=parser.add_argument_group("output_params")
    output_params.add_argument('--predictions_pickle',help='name of pickle to save predictions',default=None)
    output_params.add_argument('--predictions_hdf5',help='name of hdf5 to save predictions',default=None)
    output_params.add_argument('--performance_metrics_classification_file',help='file name to save accuracy metrics; accuracy metrics not computed if file not provided',default=None)
    output_params.add_argument('--performance_metrics_regression_file',help='file name to save accuracy metrics; accuracy metrics not computed if file not provided',default=None)
    output_params.add_argument('--performance_metrics_profile_file',help='file name to save accuracy metrics; accuracy metrics not computed if file not provided',default=None)
    
    calibration_params=parser.add_argument_group("calibration_params")
    calibration_params.add_argument("--calibrate_classification",action="store_true",default=False)
    calibration_params.add_argument("--calibrate_regression",action="store_true",default=False)        
    
    weight_params=parser.add_argument_group("weight_params")
    weight_params.add_argument('--w1',nargs="*",type=float)
    weight_params.add_argument('--w0',nargs="*",type=float)
    weight_params.add_argument("--w1_w0_file",default=None)


    model_params=parser.add_argument_group("model_params")
    model_params.add_argument('--model_hdf5',help='hdf5 file that stores the model')
    model_params.add_argument('--weights',help='weights file for the model')
    model_params.add_argument('--yaml',help='yaml file for the model')
    model_params.add_argument('--json',help='json file for the model')
    model_params.add_argument('--functional',default=False,help='use this flag if your model is a functional model',action="store_true")
    model_params.add_argument('--squeeze_input_for_gru',action='store_true')
    model_params.add_argument("--expand_dims",default=True)
    model_params.add_argument("--num_inputs",type=int)
    model_params.add_argument("--num_outputs",type=int)
    
    
    parallelization_params=parser.add_argument_group("parallelization")
    parallelization_params.add_argument("--threads",type=int,default=1)
    parallelization_params.add_argument("--max_queue_size",type=int,default=100)

    snp_params=parser.add_argument_group("snp_params")
    snp_params.add_argument('--background_freqs',default=None)
    snp_params.add_argument('--flank',default=500,type=int)
    snp_params.add_argument('--mask',default=10,type=int)
    snp_params.add_argument('--ref_col',type=int,default=None)
    snp_params.add_argument('--alt_col',type=int,default=None)

    parser.add_argument('--batch_size',type=int,help='batch size to use to make model predictions',default=50)
    return parser.parse_args()

def get_model(args):
    from kerasAC.metrics import recall, specificity, fpr, fnr, precision, f1    
    custom_objects={"recall":recall,
                    "sensitivity":recall,
                    "specificity":specificity,
                    "fpr":fpr,
                    "fnr":fnr,
                    "precision":precision,
                    "f1":f1,
                    "ambig_binary_crossentropy":ambig_binary_crossentropy,
                    "ambig_mean_absolute_error":ambig_mean_absolute_error,
                    "ambig_mean_squared_error":ambig_mean_squared_error}
    
    w1,w0=get_weights(args)
    if type(w1) in [np.ndarray, list]: 
        loss_function=get_weighted_binary_crossentropy(w0,w1)
        custom_objects["weighted_binary_crossentropy"]=loss_function
    if args.yaml!=None:
        from keras.models import model_from_yaml
        #load the model architecture from yaml
        yaml_string=open(args.yaml,'r').read()
        model=model_from_yaml(yaml_string,custom_objects=custom_objects) 
        #load the model weights
        model.load_weights(args.weights)
    elif args.json!=None:
        from keras.models import model_from_json
        #load the model architecture from json
        json_string=open(args.json,'r').read()
        model=model_from_json(json_string,custom_objects=custom_objects)
        model.load_weights(args.weights)
    elif args.model_hdf5!=None: 
        #load from the hdf5
        from keras.models import load_model
        model=load_model(args.model_hdf5,custom_objects=custom_objects)
    print("got model architecture")
    print("loaded model weights")   
    return model

def get_predictions(args,model):
    if args.variant_bed is not None:
        predictions=get_predictions_variant(args,model)
    elif args.tdb_indexer is not None:        
        predictions=get_predictions_tiledb(args,model)
    else:
        predictions=get_predictions_bed(args,model) 
    print('got model predictions')
    return predictions


def get_model_layer_functor(model,target_layer_idx):
    from keras import backend as K
    inp=model.input
    outputs=model.layers[target_layer_idx].output
    functor=K.function([inp], [outputs])
    return functor 

def get_layer_outputs(functor,X):
    return functor([X])

def calibrate(predictions,args,model):
    assert not ((args.calibrate_classification==True) and (args.calibrate_regression==True))
    if ((args.calibrate_classification==False) and (args.calibrate_regression==False)):
        print("not calibrating predictions")
        return predictions
    preacts=predictions[2]
    labels=predictions[1].values
    #perform calibration for each task!
    calibrated_predictions=None
    for i in range(preacts.shape[1]):
        #don't calibrate on nan inputs
        nonambiguous_indices=np.argwhere(~np.isnan(labels[:,i]))
        if args.calibrate_classification==True:
            calibration_func = PlattScaling()(valid_preacts=preacts[nonambiguous_indices,i],
                                                             valid_labels=labels[nonambiguous_indices,i])
        elif args.calibrate_regression==True:
            calibration_func=IsotonicRegression()(valid_preacts=preacts[nonambiguous_indices,i].squeeze(),
                                                  valid_labels=labels[nonambiguous_indices,i].squeeze())
        calibrated_predictions_task=calibration_func(preacts[:,i])
        if calibrated_predictions is None:
            calibrated_predictions=np.expand_dims(calibrated_predictions_task,axis=1)
        else:
            calibrated_predictions=np.concatenate((calibrated_predictions,np.expand_dims(calibrated_predictions_task,axis=1)),axis=1)
    predictions['calibrated_predictions']=calibrated_predictions
    return predictions

def predict(args):
    if type(args)==type({}):
        args=args_object_from_args_dict(args) 
    
    #get the predictions
    if args.predictions_pickle_to_load!=None:
        #load the pickled predictions
        with open(args.predictions_pickle_to_load,'rb') as handle:
            predictions=pickle.load(handle)
        print("loaded predictions from pickle") 
    else:
        #get the model
        model=get_model(args)
        predictions=get_predictions(args,model)
    
    #calibrate predictions (if requested by user)
    predictions=calibrate(predictions,args,model)

    #pickle the predictions if a pickle argument is provided to the code 
    if args.predictions_pickle is not None:
        print("writing predictions to pickle")
        with open(args.predictions_pickle,'wb') as handle:
            pickle.dump(predictions,handle,protocol=pickle.HIGHEST_PROTOCOL)
        print("pickled the model predictions to file:"+str(args.predictions_pickle))
        
    if ((args.performance_metrics_classification_file!=None) or (args.performance_metrics_regression_file!=None)) or (args.performance_metrics_profile_file!=None):
        #getting performance metrics
        get_performance_metrics(args)
    return

def calibrate(args):
    if type(args)==type({}):
        args=args_object_from_args_dict(args) 
    pass

def main():
    args=parse_args()
    calibrate(args) 

if __name__=="__main__":
    main()
    