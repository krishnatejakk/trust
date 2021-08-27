# -*- coding: utf-8 -*-
"""trust_auto_labeling_demo_cifar10.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1Jv2lLGlmqvRAJhyfWZnq78wLfyD2fznT

# Targeted Selection Demo For Auto Labeling

### Imports
"""

import time
import random
import datetime
import copy
import numpy as np
from tabulate import tabulate
import os
import csv
import json
import subprocess
import sys
import PIL.Image as Image
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torchvision.models as models
from matplotlib import pyplot as plt
from trust.utils.models.resnet import ResNet18
from trust.utils.custom_dataset import load_dataset_custom
from torch.utils.data import Subset
from torch.autograd import Variable
import tqdm
import argparse
from math import floor
from sklearn.metrics.pairwise import cosine_similarity, pairwise_distances
from trust.strategies.smi import SMI
from trust.strategies.partition_strategy import PartitionStrategy
from trust.strategies.random_sampling import RandomSampling


parser = argparse.ArgumentParser(description='Device ID and Class Count')
parser.add_argument('--device_id', type=int, default=0,
                    help='CUDA Device ID')
parser.add_argument('--per_cls_cnt', type=int, default=100,
                    help='Number of samples per class')

args = parser.parse_args()

seed = 42
torch.manual_seed(seed)
np.random.seed(seed)
random.seed(seed)
from trust.utils.utils import *
from trust.utils.viz import tsne_smi



"""### Helper functions"""


def model_eval_loss(data_loader, model, criterion):
    total_loss = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(data_loader):
            inputs, targets = inputs.to(device), targets.to(device, non_blocking=True)
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            total_loss += loss.item()
    return total_loss


def init_weights(m):
    if isinstance(m, nn.Conv2d):
        torch.nn.init.xavier_uniform_(m.weight)
    elif isinstance(m, nn.Linear):
        torch.nn.init.xavier_uniform_(m.weight)
        m.bias.data.fill_(0.01)


def weight_reset(m):
    if isinstance(m, nn.Conv2d) or isinstance(m, nn.Linear):
        m.reset_parameters()


def create_model(name, num_cls, device, embedding_type):
    if name == 'ResNet18':
        if embedding_type == "gradients":
            model = ResNet18(num_cls)
        else:
            model = models.resnet18()
    elif name == 'MnistNet':
        model = MnistNet()
    elif name == 'ResNet164':
        model = ResNet164(num_cls)
    model.apply(init_weights)
    model = model.to(device)
    return model


def loss_function():
    criterion = nn.CrossEntropyLoss()
    criterion_nored = nn.CrossEntropyLoss(reduction='none')
    return criterion, criterion_nored


def optimizer_with_scheduler(model, num_epochs, learning_rate, m=0.9, wd=5e-4):
    optimizer = optim.SGD(model.parameters(), lr=learning_rate,
                          momentum=m, weight_decay=wd)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=num_epochs)
    return optimizer, scheduler


def optimizer_without_scheduler(model, learning_rate, m=0.9, wd=5e-4):
    #     optimizer = optim.Adam(model.parameters(),weight_decay=wd)
    optimizer = optim.SGD(model.parameters(), lr=learning_rate,
                          momentum=m, weight_decay=wd)
    return optimizer


def generate_cumulative_timing(mod_timing):
    tmp = 0
    mod_cum_timing = np.zeros(len(mod_timing))
    for i in range(len(mod_timing)):
        tmp += mod_timing[i]
        mod_cum_timing[i] = tmp
    return mod_cum_timing / 3600


def displayTable(val_err_log, tst_err_log):
    col1 = [str(i) for i in range(10)]
    val_acc = [str(100 - i) for i in val_err_log]
    tst_acc = [str(100 - i) for i in tst_err_log]
    table = [col1, val_acc, tst_acc]
    table = map(list, zip(*table))
    print(tabulate(table, headers=['Class', 'Val Accuracy', 'Test Accuracy'], tablefmt='orgtbl'))


def find_err_per_class(test_set, val_set, final_val_classifications, final_val_predictions, final_tst_classifications,
                       final_tst_predictions, saveDir, prefix):
    val_err_idx = list(np.where(np.array(final_val_classifications) == False)[0])
    tst_err_idx = list(np.where(np.array(final_tst_classifications) == False)[0])
    val_class_err_idxs = []
    tst_err_log = []
    val_err_log = []
    for i in range(num_cls):
        if (feature == "classimb"): tst_class_idxs = list(
            torch.where(torch.Tensor(test_set.targets) == i)[0].cpu().numpy())
        val_class_idxs = list(torch.where(torch.Tensor(val_set.targets.float()) == i)[0].cpu().numpy())
        # err classifications per class
        val_err_class_idx = set(val_err_idx).intersection(set(val_class_idxs))
        tst_err_class_idx = set(tst_err_idx).intersection(set(tst_class_idxs))
        if (len(val_class_idxs) > 0):
            val_error_perc = round((len(val_err_class_idx) / len(val_class_idxs)) * 100, 2)
        else:
            val_error_perc = 0
        tst_error_perc = round((len(tst_err_class_idx) / len(tst_class_idxs)) * 100, 2)
        #         print("val, test error% for class ", i, " : ", val_error_perc, tst_error_perc)
        val_class_err_idxs.append(val_err_class_idx)
        tst_err_log.append(tst_error_perc)
        val_err_log.append(val_error_perc)
    displayTable(val_err_log, tst_err_log)
    tst_err_log.append(sum(tst_err_log) / len(tst_err_log))
    val_err_log.append(sum(val_err_log) / len(val_err_log))
    return tst_err_log, val_err_log, val_class_err_idxs


def aug_train_subset(train_set, lake_set, true_lake_set, subset, lake_subset_idxs, budget, augrandom=False):
    all_lake_idx = list(range(len(lake_set)))
    if (not (len(subset) == budget) and augrandom):
        print("Budget not filled, adding ", str(int(budget) - len(subset)), " randomly.")
        remain_budget = int(budget) - len(subset)
        remain_lake_idx = list(set(all_lake_idx) - set(subset))
        random_subset_idx = list(np.random.choice(np.array(remain_lake_idx), size=int(remain_budget), replace=False))
        subset += random_subset_idx
    lake_ss = SubsetWithTargets(true_lake_set, subset, torch.Tensor(true_lake_set.targets.float())[subset])
    remain_lake_idx = list(set(all_lake_idx) - set(lake_subset_idxs))
    remain_lake_set = SubsetWithTargets(lake_set, remain_lake_idx,
                                        torch.Tensor(lake_set.targets.float())[remain_lake_idx])
    remain_true_lake_set = SubsetWithTargets(true_lake_set, remain_lake_idx,
                                             torch.Tensor(true_lake_set.targets.float())[remain_lake_idx])
    #     print(len(lake_ss),len(remain_lake_set),len(lake_set))
    aug_train_set = torch.utils.data.ConcatDataset([train_set, lake_ss])
    return aug_train_set, remain_lake_set, remain_true_lake_set, lake_ss


def getQuerySet(val_set, val_class_err_idxs, imb_cls_idx, miscls):
    miscls_idx = []
    if (miscls):
        for i in range(len(val_class_err_idxs)):
            if i in imb_cls_idx:
                miscls_idx += val_class_err_idxs[i]
        print("Total misclassified examples from imbalanced classes (Size of query set): ", len(miscls_idx))
    else:
        for i in imb_cls_idx:
            imb_cls_samples = list(torch.where(torch.Tensor(val_set.targets.float()) == i)[0].cpu().numpy())
            miscls_idx += imb_cls_samples
        print("Total samples from imbalanced classes as targets (Size of query set): ", len(miscls_idx))
    return Subset(val_set, miscls_idx), val_set.targets[miscls_idx]


def getPerClassSel(lake_set, subset, num_cls):
    perClsSel = []
    subset_cls = torch.Tensor(lake_set.targets.float())[subset]
    for i in range(num_cls):
        cls_subset_idx = list(torch.where(subset_cls == i)[0].cpu().numpy())
        perClsSel.append(len(cls_subset_idx))
    return perClsSel


def print_final_results(res_dict, sel_cls_idx):
    print("Gain in overall test accuracy: ", res_dict['test_acc'][1] - res_dict['test_acc'][0])
    bf_sel_cls_acc = np.array(res_dict['all_class_acc'][0])[sel_cls_idx]
    af_sel_cls_acc = np.array(res_dict['all_class_acc'][1])[sel_cls_idx]
    print("Gain in targeted test accuracy: ", np.mean(af_sel_cls_acc - bf_sel_cls_acc))


"""# Data, Model & Experimental Settings
The CIFAR-10 dataset contains 60,000 32x32 color images in 10 different classes.The 10 different classes represent airplanes, cars, birds, cats, deer, dogs, frogs, horses, ships, and trucks. There are 6,000 images of each class. The training set contains 50,000 images and test set contains 10,000 images. We will use custom_dataset() function in Trust to simulated a class imbalance scenario using the split_cfg dictionary given below. We then use a ResNet18 model as our task DNN and train it on the simulated imbalanced version of the CIFAR-10 dataset. Next we perform targeted selection using various SMI functions and compare their gain in overall accuracy as well as on the imbalanced classes.
"""

#cls_cnts = [25, 50, 100, 250, 500, 750, 1000]
#budgets = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000]
budgets = [5000]
#for per_cls_cnt in cls_cnts:
for budget in budgets:
    feature = "classimb"
    device_id = args.device_id
    run = "test_run"
    datadir = 'data/'
    data_name = 'cifar10'
    model_name = 'ResNet18'
    learning_rate = 0.01
    computeClassErrorLog = True
    device = "cuda:" + str(device_id) if torch.cuda.is_available() else "cpu"
    miscls = False  # Set to True if only the misclassified examples from the imbalanced classes is to be used
    embedding_type = "gradients"  # Type of the representation to use (gradients/features)
    num_cls = 10
    #budget = 5000
    per_cls_cnt = args.per_cls_cnt
    visualize_tsne = False
    split_cfg = {"sel_cls_idx": [0],  # Class of the query set
                "per_class_train": {0: per_cls_cnt, 1: per_cls_cnt, 2: per_cls_cnt, 3: per_cls_cnt, 4: per_cls_cnt,
                                    5: per_cls_cnt, 6: per_cls_cnt, 7: per_cls_cnt, 8: per_cls_cnt, 9: per_cls_cnt},
                "per_class_val": {0: 20, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0, 9: 0},
                "per_class_lake": {0: 5000, 1: 5000, 2: 5000, 3: 5000, 4: 5000, 5: 5000, 6: 5000, 7: 5000, 8: 5000,
                                    9: 5000},
                "per_class_test": {0: 100, 1: 100, 2: 100, 3: 100, 4: 100, 5: 100, 6: 100, 7: 100, 8: 100, 9: 100}}
    initModelPath = "./" + data_name + "_" + model_name + "_" + str(learning_rate) + "_" + str(split_cfg["sel_cls_idx"])


    def weak_labeling(dataset_name, datadir, feature, model_name, budget, split_cfg, num_cls, learning_rate, run,
                    device, computeErrorLog, strategy="SIM", sf=""):
        resultsPath = "./" + data_name + "_" + model_name + "_" + str(per_cls_cnt) + "_" + str(budget) + "_" + strategy + "_" + sf + ".json"
        # load the dataset in the class imbalance setting
        train_set, val_set, test_set, lake_set, sel_cls_idx, num_cls = load_dataset_custom(datadir, dataset_name, feature,
                                                                                        split_cfg, False, False)
        val_sets = []
        for i in range(num_cls):
            tmp_indices = torch.where(train_set.targets == i)[0]
            tmp_set = SubsetWithTargets(train_set.dataset.dataset, [train_set.dataset.indices[x] for x in tmp_indices],
                                        train_set.targets[tmp_indices])
            val_sets.append(tmp_set)

        print("Indices of randomly selected classes for imbalance: ", sel_cls_idx)

        # Set batch size for train, validation and test datasets
        N = len(train_set)
        trn_batch_size = 20
        val_batch_size = 10
        tst_batch_size = 100

        # Create dataloaders
        trainloader = torch.utils.data.DataLoader(train_set, batch_size=trn_batch_size,
                                                shuffle=True, pin_memory=True)

        valloaders = []

        for i in range(num_cls):
            valloader = torch.utils.data.DataLoader(val_sets[i], batch_size=val_batch_size,
                                                    shuffle=False, pin_memory=True)
            valloaders.append(valloader)

        tstloader = torch.utils.data.DataLoader(test_set, batch_size=tst_batch_size,
                                                shuffle=False, pin_memory=True)

        lakeloader = torch.utils.data.DataLoader(lake_set, batch_size=tst_batch_size,
                                                shuffle=False, pin_memory=True)

        true_lake_set = copy.deepcopy(lake_set)
        # Budget for subset selection
        bud = budget
        # Variables to store accuracies
        num_rounds = 1  # The first round is for training the initial model and the second round is to train the final model
        full_trn_acc = 0
        
        # Model Creation
        model = create_model(model_name, num_cls, device, embedding_type)
        
        # Loss Functions
        criterion, criterion_nored = loss_function()
        # Getting the optimizer and scheduler
        optimizer = optimizer_without_scheduler(model, learning_rate)

        strategy_args = {'batch_size': 20, 'device': device, 'embedding_type': 'gradients', 'keep_embedding': True, 'wrapped_strategy_class': SMI, 'num_partitions': 10, 'loss': torch.nn.functional.cross_entropy}
        unlabeled_lake_set = LabeledToUnlabeledDataset(lake_set)

        if (strategy == "SIM"):
            strategy_args['smi_function'] = sf
            weak_labelers = []
            for i in range(num_cls):
                strategy_sel = PartitionStrategy(train_set, unlabeled_lake_set, model, num_cls, strategy_args, query_dataset=val_sets[i])
                weak_labelers.append(strategy_sel)

        if (strategy == "random"):
            weak_labelers = []
            strategy_sel = RandomSampling(train_set, unlabeled_lake_set, model, num_cls, strategy_args)
            weak_labelers.append(strategy_sel)


        ###Model Pre-training###
        start_time = time.time()
        num_ep = 1
        while (full_trn_acc < 0.99 and num_ep < 300):
            model.train()
            for batch_idx, (inputs, targets) in enumerate(trainloader):
                inputs, targets = inputs.to(device), targets.to(device, non_blocking=True)
                # Variables in Pytorch are differentiable.
                inputs, target = Variable(inputs), Variable(inputs)
                # This will zero out the gradients for this batch.
                optimizer.zero_grad()
                outputs = model(inputs)
                loss = criterion(outputs, targets)
                loss.backward()
                optimizer.step()
            #             scheduler.step()

            full_trn_loss = 0
            full_trn_correct = 0
            full_trn_total = 0
            model.eval()
            with torch.no_grad():
                for batch_idx, (inputs, targets) in enumerate(trainloader):  # Compute Train accuracy
                    inputs, targets = inputs.to(device), targets.to(device, non_blocking=True)
                    outputs = model(inputs)
                    loss = criterion(outputs, targets)
                    full_trn_loss += loss.item()
                    _, predicted = outputs.max(1)
                    full_trn_total += targets.size(0)
                    full_trn_correct += predicted.eq(targets).sum().item()
                full_trn_acc = full_trn_correct / full_trn_total
                print("Selection Epoch ", i, " Training epoch [", num_ep, "]", " Training Acc: ", full_trn_acc, end="\r")
                num_ep += 1
            timing = time.time() - start_time

        unlabeled_lake_set = LabeledToUnlabeledDataset(lake_set)

        subsets = []
        cnt = 0
        for strategy_sel in weak_labelers:
            print("Class: " + str(cnt) + "\n")
            strategy_sel.update_data(train_set, unlabeled_lake_set)
            strategy_sel.update_model(model)
            subset, gain = strategy_sel.select(budget)
            subset = [x for _, x in sorted(zip(gain, subset), key=lambda pair: pair[0], reverse=True)]
            subsets.append(subset)
            cnt += 1
        # ####SIM####
        # if (strategy == "SIM" or strategy == "SF"):
        #     if (sf.endswith("mi")):
        #         if (feature == "classimb"):
        #             # make a dataloader for the misclassifications - only for experiments with targets
        #             miscls_set, miscls_set_targets = getQuerySet(val_set, val_class_err_idxs, sel_cls_idx, miscls)
        #             strategy_sel.update_queries(miscls_set)

        print("#### Selection Complete, Now re-training with augmented subset ####")
        results_dict = dict()
        
        for j in range(10):
            budget_dict = dict()
            for i in range(num_cls):
                tmp_subset = subsets[i][0:500*(j+1)]
                tmp_targets = true_lake_set.targets[tmp_subset]
                tmp_metrics = (len(torch.where(tmp_targets == i)[0])/len(tmp_targets))
                budget_dict[i] = tmp_metrics
            results_dict[500 * (j+1)] = budget_dict

        with open(resultsPath, 'w') as json_file:
            json.dump(results_dict, json_file)


    """# Submodular Mutual Information (SMI)
    
    We let $V$ denote the ground-set of $n$ data points $V = \{1, 2, 3,...,n \}$ and a set function $f:
    2^{V} xrightarrow{} \Re$. Given a set of items $A, B \subseteq V$, the submodular mutual information (MI)[1,3] is defined as $I_f(A; B) = f(A) + f(B) - f(A \cup B)$. Intuitively, this measures the similarity between $B$ and $A$ and we refer to $B$ as the query set.
    
    In [2], they extend MI to handle the case when the target can come from an auxiliary set $V^{\prime}$ different from the ground set $V$. For targeted data subset selection, $V$ is the source set of data instances and the target is a subset of data points (validation set or the specific set of examples of interest).
    Let $\Omega  = V \cup V^{\prime}$. We define a set function $f: 2^{\Omega} \rightarrow \Re$. Although $f$ is defined on $\Omega$, the discrete optimization problem will only be defined on subsets $A \subseteq V$. To find an optimal subset given a query set $Q \subseteq V^{\prime}$, we can define $g_{Q}(A) = I_f(A; Q)$, $A \subseteq V$ and maximize the same.
    
    # FL1MI
    
    In the first variant of FL, we set the unlabeled dataset to be $V$. The SMI instantiation of FL1MI can be defined as:
    \begin{align}
    I_f(A;Q)=\sum_{i \in V}\min(\max_{j \in A}s_{ij}, \eta \max_{j \in Q}sq_{ij})
    \end{align}
    
    The first term in the min(.) of FL1MI models diversity, and the second term models query relevance. An increase in the value of $\eta$ causes the resulting summary to become more relevant to the query.
    """

    weak_labeling(data_name,
                datadir,
                feature,
                model_name,
                budget,
                split_cfg,
                num_cls,
                learning_rate,
                run,
                device,
                computeClassErrorLog,
                "SIM", 'fl1mi')

    """# FL2MI
    
    In the V2 variant, we set $D$ to be $V \cup Q$. The SMI instantiation of FL2MI can be defined as:
    \begin{align} \label{eq:FL2MI}
    I_f(A;Q)=\sum_{i \in Q} \max_{j \in A} sq_{ij} + \eta\sum_{i \in A} \max_{j \in Q} sq_{ij}
    \end{align}
    FL2MI is very intuitive for query relevance as well. It measures the representation of data points that are the most relevant to the query set and vice versa. It can also be thought of as a bidirectional representation score.
    """

    weak_labeling(data_name,
                datadir,
                feature,
                model_name,
                budget,
                split_cfg,
                num_cls,
                learning_rate,
                run,
                device,
                computeClassErrorLog,
                "SIM", 'fl2mi')

    """# GCMI
    
    The SMI instantiation of graph-cut (GCMI) is defined as:
    \begin{align}
    I_f(A;Q)=2\sum_{i \in A} \sum_{j \in Q} sq_{ij}
    \end{align}
    Since maximizing GCMI maximizes the joint pairwise sum with the query set, it will lead to a subset similar to the query set $Q$.
    """

    weak_labeling(data_name,
                datadir,
                feature,
                model_name,
                budget,
                split_cfg,
                num_cls,
                learning_rate,
                run,
                device,
                computeClassErrorLog,
                "SIM", 'gcmi')

    """# LOGDETMI
    
    The SMI instantiation of LogDetMI can be defined as:
    \begin{align}
    I_f(A;Q)=\log\det(S_{A}) -\log\det(S_{A} - \eta^2 S_{A,Q}S_{Q}^{-1}S_{A,Q}^T)
    \end{align}
    $S_{A, B}$ denotes the cross-similarity matrix between the items in sets $A$ and $B$. The similarity matrix in constructed in such a way that the cross-similarity between $A$ and $Q$ is multiplied by $\eta$ to control the trade-off between query-relevance and diversity.
    """

    weak_labeling(data_name,
                datadir,
                feature,
                model_name,
                budget,
                split_cfg,
                num_cls,
                learning_rate,
                run,
                device,
                computeClassErrorLog,
                "SIM", 'logdetmi')

    """# Random"""

    # weak_labeling(data_name,
    #               datadir,
    #               feature,
    #               model_name,
    #               budget,
    #               split_cfg,
    #               num_cls,
    #               learning_rate,
    #               run,
    #               device,
    #               computeClassErrorLog,
    #               "random", 'random')

"""# References
[1] Rishabh Iyer, Ninad Khargoankar, Jeff Bilmes, and Himanshu Asnani. Submodular combinatorialinformation measures with applications in machine learning.arXiv preprint arXiv:2006.15412,2020


[2] Kaushal V, Kothawade S, Ramakrishnan G, Bilmes J, Iyer R. PRISM: A Unified Framework of Parameterized Submodular Information Measures for Targeted Data Subset Selection and Summarization. arXiv preprint arXiv:2103.00128. 2021 Feb 27.


[3] Anupam Gupta and Roie Levin. The online submodular cover problem. InACM-SIAM Symposiumon Discrete Algorithms, 2020
"""

