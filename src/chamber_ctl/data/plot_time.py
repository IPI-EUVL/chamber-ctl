import numpy as np
import plotly.graph_objects as go
import argparse


def load_file(path):
    val = np.load(path)
    val = val["arr_0"]

    return val[:, 0], val[:, 1]

def add_plot_time(fig, time, dose, name):
    last_time = time[-1]
    last_dose = dose[-1]

    dose_rate = last_dose / last_time

    fig.add_trace(go.Scatter(x=time, y=dose, mode='lines', name=f"{name}: {last_dose:.4f} mJ/cm^2 at {last_time:.4f} s ({dose_rate:.4f} mJ/cm^2/s)"))


def main():
    parser = argparse.ArgumentParser(description="Calculate dose of an experiment.")
    parser.add_argument("filenames", type=str, help="Input files")
    args = parser.parse_args()
    
    filenames = args.filenames.split(",")

    fig2 = go.Figure()


    for filename_str in filenames:
        filename, name = filename_str.split(":")
        time, dose = load_file(filename)
        add_plot_time(fig2, time, dose, name)

    fig2.show()

if __name__ == "__main__":
    main()