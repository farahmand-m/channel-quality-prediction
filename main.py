import argparse

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as func
import torch.optim as optim

from model import Network
from protocols import tsch, enhanced_tsch, intelligent_tsch
from simulation import BitErrorProb, PacketReceptionProb


def reduce(values, method, matrix=None):
    if method == 'mean':
        channels_sums = torch.sum(values, dim=0)
        channels_weights = torch.sum(matrix, dim=0)
        return channels_sums / channels_weights
    values, indices = torch.max(values, dim=0)
    return values


def describe(receptions, title, ed_enabled=True):
    print(title)
    prr = torch.mean(receptions[train_samples_cutoff: 300 * args.sample_rate])
    n_ed, n_rx, n_tx = 3 if ed_enabled else 0, 7, 1
    i_ed, i_rx, i_tx = 5e-3, 5e-3, 10e-3
    t_ed, t_tx = 128e-6,  3.2e-3
    v_cc = 3.3
    energy = (i_ed * n_ed * t_ed + (i_rx * n_rx * t_tx + i_tx * n_tx * t_tx) / prr) * v_cc
    print('PRR: {:.4f}, Energy Consumption: {:.2f} uJ'.format(prr, energy * 1e6))


if __name__ == '__main__':
    # Adjusting the DPI of the figures.
    mpl.rcParams.update({'figure.dpi': 300, 'font.size': 7})
    # Command Line Arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', choices=('apartments', 'downtown', 'suburb'))
    parser.add_argument('--train', action='store_true', default=False, help='if not specified, loads saved model')
    parser.add_argument('--train-split', type=int, default=240, help='in seconds')
    parser.add_argument('--sample-rate', type=int, default=2000, help='dataset sample rate')
    parser.add_argument('--target-rate', type=int, default=10, help='sample rate of the data fed to the network')
    parser.add_argument('--power', type=float, default=-10., help='tran. power in dBm')
    parser.add_argument('--alpha', type=float, default=3.5, help='path-loss exponent')
    parser.add_argument('--distance', type=float, default=3., help='assumed to be 3 in Eqn. 5')
    parser.add_argument('--packet-length', type=int, default=133, help='in bytes')
    parser.add_argument('--past-window', type=int, default=5, help='denoted as t_p')
    parser.add_argument('--future-window', type=int, default=5, help='denoted as t_f')
    parser.add_argument('--layers', type=int, default=2, help='number of recurrent layers')
    parser.add_argument('--neurons', type=int, default=50, help='number of recurrent neurons')
    parser.add_argument('--iterations', type=int, default=1000, help='number of iterations to train for')
    parser.add_argument('--batch-size', type=int, default=32, help='training batch size')
    args = parser.parse_args()
    # Dataset Preparation
    data = torch.load(f'data/{args.dataset}.pt')
    datapoints, sequences, channels = data.shape
    train_samples_cutoff = args.train_split * args.sample_rate
    training_data = data[:train_samples_cutoff]
    validation_data = data[train_samples_cutoff:]
    data_mean, data_std = torch.mean(training_data), torch.std(training_data)
    # Modules Setup
    criterion = nn.BCELoss()
    error_props = BitErrorProb(args.power, args.alpha, args.distance)
    reception_props = PacketReceptionProb(args.packet_length)
    models = {'mean': (0.05, Network(args.layers, args.neurons)), 'max': (0.55, Network(args.layers, args.neurons))}
    # Whether to train a new model or load pre-trained ones.
    if args.train:
        # Training a New Model
        for reducing_method, (penalty_weight, network) in models.items():
            optimizer = optim.RMSprop(network.parameters(), lr=1e-4)
            metrics = list()
            network.train()
            for iteration in range(args.iterations):
                optimizer.zero_grad()
                # Selecting a Random Batch
                random_indices = np.random.randint(args.past_window * args.sample_rate, int(0.80 * datapoints) - args.future_window * args.sample_rate, args.batch_size)
                past_windows = torch.cat([training_data[index - args.past_window * args.sample_rate: index] for index in random_indices], dim=1)
                future_windows = torch.cat([training_data[index: index + args.future_window * args.sample_rate] for index in random_indices], dim=1)
                # Forward Pass through the Network Module
                past_windows_downsampled = func.interpolate(past_windows.permute(1, 2, 0), scale_factor=args.target_rate / args.sample_rate, mode='linear').permute(2, 0, 1)
                past_windows_normalized = (past_windows_downsampled - data_mean) / data_std
                blacklist = network(past_windows_normalized)
                # Forward Pass through the Simulation Module
                interference_power_levels, channels_matrix = tsch(future_windows)
                error_prop_values = error_props(interference_power_levels)
                error_props_per_channel = torch.mul(error_prop_values.unsqueeze(dim=2), channels_matrix)
                errors_reduced = reduce(error_props_per_channel, reducing_method, channels_matrix)
                # Loss Function
                whitelist = torch.ones_like(blacklist) - blacklist
                outputs = torch.mul(errors_reduced, whitelist)
                desired_outputs = torch.zeros_like(outputs)
                cross_entropy_loss = criterion(outputs, desired_outputs)
                blacklisting_penalty = torch.mean(blacklist)
                loss_func = cross_entropy_loss + blacklisting_penalty * penalty_weight
                # Backward Pass
                loss_func.backward()
                # Optimization Step
                optimizer.step()
                # Logging
                iteration_metrics = (cross_entropy_loss.item(), blacklisting_penalty.item(), loss_func.item())
                metrics.append(iteration_metrics)
                print('Iteration {}/{} Cross-Entropy Loss: {:.4f} Blacklisting Penalty: {:.4f} Total Loss: {:.4f}'.format(
                    iteration + 1, args.iterations, *iteration_metrics
                ))
            files_path = f'results/{args.dataset}-{reducing_method}'
            torch.save(network, files_path + '.pt')
            metrics = np.array(metrics)
            # noinspection PyTypeChecker
            figure, axes = plt.subplots(3, 1, figsize=(5, 9), sharex=True)
            for dim, name in enumerate(('Cross-Entropy Loss', 'Penalty', 'Total Loss')):
                axes[dim].set_title(name)
                axes[dim].plot(metrics[:, dim])
            plt.tight_layout()
            plt.savefig(files_path + '-metrics.png')
            plt.show()
    else:
        # Loading already-trained Models
        for reducing_method, (penalty_weight, network) in models.items():
            model_path = f'results/{args.dataset}-{reducing_method}.pt'
            models[reducing_method] = (penalty_weight, torch.load(model_path))
    # Evaluation
    print(f'[{args.dataset.capitalize()}]')
    tsch_interference, tsch_channels_matrix = tsch(data)
    tsch_errors = error_props(tsch_interference)
    tsch_receptions = reception_props(tsch_errors)
    describe(tsch_receptions, 'Standard TSCH', ed_enabled=False)
    enhanced_tsch_interference = enhanced_tsch(data, args.target_rate / args.sample_rate)
    enhanced_tsch_errors = error_props(enhanced_tsch_interference)
    enhanced_tsch_receptions = reception_props(enhanced_tsch_errors)
    describe(enhanced_tsch_receptions, 'Enhanced TSCH')
    # noinspection PyTypeChecker
    figure, axis = plt.subplots(sharex=True, sharey=True, figsize=(4, 2.5))
    width = 0.5  # Adjust me!
    axis.plot(tsch_receptions.numpy().flatten(), label='Standard TSCH', color='gray', linewidth=width)
    axis.plot(enhanced_tsch_receptions.numpy().flatten(), label='Enhanced TSCH', color='orange', linewidth=width)
    for reducing_method, (penalty_weight, network) in models.items():
        network.eval()
        pivots = np.arange(args.past_window * args.sample_rate, datapoints, args.future_window * args.sample_rate)
        past_windows = torch.cat([data[index - args.past_window * args.sample_rate: index] for index in pivots], dim=1)
        future_windows = torch.cat([data[index: index + args.future_window * args.sample_rate] for index in pivots], dim=1)
        past_windows_downsampled = func.interpolate(past_windows.permute(1, 2, 0), scale_factor=args.target_rate / args.sample_rate, mode='linear').permute(2, 0, 1)
        past_windows_normalized = (past_windows_downsampled - data_mean) / data_std
        with torch.no_grad():
            blacklist = network(past_windows_normalized)
            scores_sorted = np.sort(blacklist)
            thresholds = scores_sorted[:, 8]
            thresholds = np.expand_dims(thresholds, axis=1)
            available_channels = blacklist < torch.from_numpy(thresholds)
            interference_values = intelligent_tsch(future_windows, available_channels)
            error_props_values = error_props(interference_values)
            reception_props_values = reception_props(torch.cat((tsch_errors[:args.past_window * args.sample_rate], error_props_values.permute(1, 0).view(-1, 1)), dim=0))
        describe(reception_props_values, 'ITSCH ({})'.format(reducing_method))
        axis.plot(reception_props_values, label='ITSCH ({})'.format(reducing_method), color='green' if reducing_method == 'mean' else 'blue', linewidth=width)
        axis.set_xlabel('Time (Sec)')
        axis.set_ylabel('PRP')
        axis.set_xticks(np.arange(0, datapoints + 1, 15 * args.sample_rate))
        axis.set_xticklabels(np.arange(0, datapoints / args.sample_rate + 1, 15).astype('int'))
        axis.set_xlim(train_samples_cutoff, min(len(reception_props_values), 300 * args.sample_rate))
        axis.set_ylim(round(tsch_receptions.numpy().flatten()[train_samples_cutoff: min(len(reception_props_values), 300 * args.sample_rate)].min(), 1), 1.01)
        axis.set_title(args.dataset.capitalize())
    plt.gcf().subplots_adjust(left=0.15, bottom=0.18)
    plt.show()
