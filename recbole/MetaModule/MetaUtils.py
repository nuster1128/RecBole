# @Time   : 2022/3/23
# @Author : Zeyu Zhang
# @Email  : wfzhangzeyu@163.com

"""
recbole.MetaModule.MetaUtils
##########################
"""

from collections import OrderedDict
import os,pickle
import torch.nn as nn
from recbole.evaluator.collector import Collector
from recbole.data.dataloader import *
from recbole.utils.argument_list import dataset_arguments
from recbole.utils import set_color

class Task():
    '''
    Task is the basis of meta learning.
    For user cold start recsys, a task usually refers to a user.
    '''
    def __init__(self,taskInfo,spt,qrt):
        '''
        Generate a new task, including task information, support set, query set.

        :param taskInfo(dict): eg. {'task_id':202} task_id always equals to user_id
        :param spt(Interaction): eg. (user_id, item_id, rating) tuples
        :param qrt(Interaction): For training eg. (user_id, item_id, rating) tuples; for testing (user_id, item_id) tuples
        '''
        self.taskInfo = taskInfo
        self.spt = spt
        self.qrt = qrt

class MetaCollector(Collector):
    '''
    MetaCollector is the key component for collect data for evaluation in meta learning circumstance.

    Overall, we extend 'Collector' to 'MetaCollector'.
    The extended modification can be listed briefly as following:

    [Override] self.eval_collect(self, eval_pred: torch.Tensor, data_label: torch.Tensor): Collect data for evaluation.

    [Override] self.data_collect(self, train_data): Collect the evaluation resource from training data.

    '''
    def __init__(self,config):
        super(MetaCollector, self).__init__(config)

    def eval_collect(self, eval_pred: torch.Tensor, data_label: torch.Tensor):
        '''
        Collect data for evaluation.

        :param eval_pred(torch.Tensor) : Normally, it is a 1D score tensor for the query prediction in a single task.
        :param data_label(torch.Tensor) : Normally, it is a 1D score tensor for the query label in a single task.
        :return:
        '''
        if self.register.need('rec.score'):
            self.data_struct.update_tensor('rec.score', eval_pred)

        if self.register.need('data.label'):
            self.label_field = self.config['LABEL_FIELD']
            self.data_struct.update_tensor('data.label', data_label.to(self.device))

        if self.register.need('rec.topk'):
            _, eval_topk_idx = torch.topk(eval_pred, max(self.topk), dim=-1)
            _, label_topk_idx = torch.topk(data_label, max(self.topk), dim=-1)

            pos_matrix = torch.zeros_like(eval_pred, dtype=torch.int)
            pos_matrix[label_topk_idx] = 1
            pos_len_list = pos_matrix.sum(dim=0,keepdim=True)
            pos_idx = torch.gather(pos_matrix, dim=0, index=eval_topk_idx)
            result = torch.cat((pos_idx, pos_len_list),dim=0)
            result=result.unsqueeze(dim=0)
            self.data_struct.update_tensor('rec.topk', result)

    def data_collect(self, train_data):
        '''
        Collect the evaluation resource from training data.

        :param train_data: The training dataloader which contains the training data
        '''
        if self.register.need('data.num_users'):
            self.data_struct.set('data.num_users', len(train_data.getUserList()))

def create_meta_dataset(config):
    '''
    This function is rewritten from 'recbole.data.create_meta_dataset(config)'
    '''
    from MetaDataset import MetaDataset

    dataset_class =MetaDataset

    default_file = os.path.join(config['checkpoint_dir'], f'{config["dataset"]}-{dataset_class.__name__}.pth')
    file = config['dataset_save_path'] or default_file
    if os.path.exists(file):
        with open(file, 'rb') as f:
            dataset = pickle.load(f)
        dataset_args_unchanged = True
        for arg in dataset_arguments + ['seed', 'repeatable']:
            if config[arg] != dataset.config[arg]:
                dataset_args_unchanged = False
                break
        if dataset_args_unchanged:
            logger = getLogger()
            logger.info(set_color('Load filtered dataset from', 'pink') + f': [{file}]')
            return dataset

    dataset = dataset_class(config)
    if config['save_dataset']:
        dataset.save()
    return dataset

def meta_data_preparation(config, dataset):
    '''
    This function is rewritten from 'recbole.data.data_preparation(config, dataset)'
    '''
    from recbole.data.utils import load_split_dataloaders, save_split_dataloaders
    from MetaDataLoader import MetaDataLoader

    dataloaders = load_split_dataloaders(config)
    if dataloaders is not None:
        train_data, valid_data, test_data = dataloaders
    else:
        built_datasets = dataset.build()

        train_dataset, valid_dataset, test_dataset = built_datasets
        # print(train_dataset.user_num)  There some problems with incorrect user number in .inter sets.

        train_sampler, valid_sampler, test_sampler = None,None,None

        train_data = MetaDataLoader(config, train_dataset, train_sampler, shuffle=True)
        valid_data = MetaDataLoader(config, valid_dataset, valid_sampler, shuffle=True)
        test_data = MetaDataLoader(config, test_dataset, test_sampler, shuffle=True)
        if config['save_dataloaders']:
            save_split_dataloaders(config, dataloaders=(train_data, valid_data, test_data))

    logger = getLogger()
    logger.info(
        set_color('[Training]: ', 'pink') + set_color('train_batch_size', 'cyan') + ' = ' +
        set_color(f'[{config["train_batch_size"]}]', 'yellow') + set_color(' negative sampling', 'cyan') + ': ' +
        set_color(f'[{config["neg_sampling"]}]', 'yellow')
    )
    logger.info(
        set_color('[Evaluation]: ', 'pink') + set_color('eval_batch_size', 'cyan') + ' = ' +
        set_color(f'[{config["eval_batch_size"]}]', 'yellow') + set_color(' eval_args', 'cyan') + ': ' +
        set_color(f'[{config["eval_args"]}]', 'yellow')
    )
    return train_data, valid_data, test_data

class GradCollector():
    '''
    This is a common data struct to collect grad.

    For the sake of complex calculation graph in meta learning, we construct this data struct to
    do grad operations on batch data.
    '''
    def __init__(self,paramsNameList):
        '''
        Initialize GradCollector Object.
        :param paramsNameList: Usually comes from list(nn.Moudule.state_dict().keys())
        '''
        self.paramNameList=paramsNameList
        self.gradDict=OrderedDict()

    def addGrad(self,gradTuple):
        '''
        Add grad and exist grad.

        :param gradTuple(tuple of torch.Tensor): Usually refers to grad tuple from 'torch.autograd.grad()'.

        '''
        for index,name in enumerate(self.paramNameList):
            if name not in self.gradDict:
                self.gradDict[name] = gradTuple[index]
            else:
                self.gradDict[name] += gradTuple[index]

    def averageGrad(self,size):
        '''
        Average operation for all grads.

        :param size: The denominator of average.

        '''
        for name,value in self.gradDict.items():
            self.gradDict[name]=self.gradDict[name]/size

    def clearGrad(self):
        '''
        Clear all grads.
        '''
        self.gradDict = OrderedDict()

    def dumpGrad(self):
        '''
        Return the grad tuple in the collector and clear all grads.

        :return grad(tuple of torch.Tensor): The grad tuple in the collector.

        '''
        grad=self.gradDict
        self.clearGrad()
        return grad

    def print(self):
        '''
        Print name and grad.shape for all parameters.
        '''
        for name,grad in self.gradDict.items():
            print(name,grad.shape)

class EmbeddingTable(nn.Module):
    '''
    This is a data struct to embedding interactions.
    It supports 'token' and 'float' type.
    '''
    def __init__(self,embeddingSize,dataset):
        super(EmbeddingTable, self).__init__()

        self.dataset=dataset
        self.embeddingSize=embeddingSize

        self.embeddingDict = dict()
        self.initialize()

    def initialize(self):
        '''
        Initialize the fields of embedding.
        '''
        self.embeddingFields = self.dataset.fields(source=[FeatureSource.USER, FeatureSource.ITEM])
        for field in self.embeddingFields:
            if self.fieldType(field) is FeatureType.TOKEN:
                self.embeddingDict[field]=nn.Embedding(self.dataset.num(field),self.embeddingSize)
                self.add_module(field,self.embeddingDict[field])

    def fieldType(self,field):
        '''
        Convert field to type.
        :param field(str): Field name.
        :return type(str): Field type.
        '''
        return self.dataset.field2type[field]

    def embeddingSingleField(self,field,batchX):
        '''
        Embedding a single field.
        If the field type is 'float' then return itself, else return the 'token' embedding vectors.

        :param field(str): Field name.
        :param batchX(torch.Tensor):  Batch of tensor.
        :return: batchX(torch.Tensor): Batch of tensor.
        '''
        if self.fieldType(field) is FeatureType.TOKEN:
            return self.embeddingDict[field](batchX)
        else:
            return batchX

    def embeddingAllFields(self,interaction):
        '''
        Embedding all fields of the interaction.
        Only fields in 'self.embeddingFields' will be embedded.

        :param interaction(Interaction): The input interaction.
        :return batchX(torch.Tensor): The concatenating process embedding of all fields.
        '''
        batchX=[]
        for field in self.embeddingFields:
            feature=self.embeddingSingleField(field,interaction[field])
            batchX.append(feature)
        batchX=torch.cat(batchX,dim=1)
        return batchX
