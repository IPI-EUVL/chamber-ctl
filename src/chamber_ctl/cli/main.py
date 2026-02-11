import argparse

from chamber_ctl.cli.experiments import main as experiments_main

def main():
    parser = argparse.ArgumentParser(description="Chamber Control CLI")
    
    subparsers = parser.add_subparsers(dest="command")
    
    exp_parser = subparsers.add_parser("experiments", help="Experiment data reader")
    exp_parser.set_defaults(func=experiments_main)

    args = parser.parse_args()
    
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()