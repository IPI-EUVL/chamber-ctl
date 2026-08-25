import argparse

from chamber_ctl.cli.experiments import main as experiments_main
from chamber_ctl.cli.acquisition import add_parser as add_acquisition_parser
from chamber_ctl.cli.calibration import add_parser as add_calibration_parser

def main():
    parser = argparse.ArgumentParser(description="Chamber Control CLI")
    
    subparsers = parser.add_subparsers(dest="command")
    
    exp_parser = subparsers.add_parser("experiments", help="Experiment data reader")
    exp_parser.set_defaults(func=experiments_main)
    add_acquisition_parser(subparsers)
    add_calibration_parser(subparsers)

    args = parser.parse_args()
    
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()