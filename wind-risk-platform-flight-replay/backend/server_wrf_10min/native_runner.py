"""Run the frozen production main/utils with d02 output-only overrides."""
from __future__ import annotations
import argparse
import runpy
import sys
from pathlib import Path


def output_options(options=None):
    # Original parent/child steps 48/16 cannot land on every 600s boundary.
    # Keep fixed stepping and all physics; 45/15 gives exact native outputs.
    return {**(options or {}),'history_interval':'60,10','frames_per_outfile':'1,1','time_step':'45'}


def run(output_dir):
    upstream=Path(__file__).resolve().parent/'upstream'
    sys.path.insert(0,str(upstream))
    import utils
    original=utils.run_wrf
    def configured(*args,**kwargs):
        values=list(args)
        if values[5]!=2: raise ValueError('Native production runner expects d01/d02')
        values[2]=str(output_dir)
        kwargs['opts']=output_options(kwargs.get('opts'))
        return original(*values,**kwargs)
    utils.run_wrf=configured
    runpy.run_path(str(upstream/'main.py'),run_name='__main__')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    run(parser.parse_args().output_dir)
