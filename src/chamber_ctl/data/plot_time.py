import matplotlib.pyplot as plt
import numpy as np
import argparse


def load_file(path):
    val = np.load(path)
    val = val["arr_0"]

    return val[:, 0], val[:, 1]

def plot_time(time, dose):
    plt.plot(time, dose)
    plt.xlabel("Time")
    plt.ylabel("Dose")
    plt.title("Dose vs Time")
    plt.show()

def main():
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("filename", type=str, help="Name of the output file")
    args = parser.parse_args()

    filename = args.filename

    if '.' in filename and not filename.endswith(".npz"):
        print("Warning: Filename has an extension that is not .npz")
    elif not filename.endswith(".npz"):
        filename += ".npz"

    time, dose = load_file(filename)
    plot_time(time, dose)

if __name__ == "__main__":
    main()