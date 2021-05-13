#!/usr/bin/env python3
from typing import Optional, List
import click
from pathlib import Path
import logging
import sys
from datetime import datetime

from sec_certs.configuration import config
from sec_certs.dataset.common_criteria import CCDataset

logger = logging.getLogger(__name__)


@click.command()
@click.argument('actions', required=True, nargs=-1, type=click.Choice(['all', 'build', 'download', 'convert', 'analyze'], case_sensitive=False))
@click.option('-o', '--output', type=click.Path(file_okay=False, dir_okay=True, writable=True, readable=True),
              help='Path where the output of the experiment will be stored. May overwrite existing content.')
@click.option('-c', '--config', 'configpath', default=None, type=click.Path(file_okay=True, dir_okay=False, writable=True, readable=True),
              help='Path to your own config yaml file that will override the default one.')
@click.option('-i', '--input', 'inputpath', type=click.Path(file_okay=True, dir_okay=False, writable=True, readable=True),
              help='If set, the actions will be performed on a CC dataset loaded from JSON from the input path.')
@click.option('-s', '--silent', is_flag=True, help='If set, will not print to stdout')
def main(configpath: Optional[str], actions: List[str], inputpath: Optional[Path], output: Optional[Path], silent: bool):
    """
    Specify actions, sequence of one or more strings from the following list: [all, build, download, convert, analyze]
    If 'all' is specified, all actions run against the dataset. Otherwise, only selected actions will run in the correct order.
    """
    file_handler = logging.FileHandler(config.log_filepath)
    stream_handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    handlers = [file_handler]

    output = Path(output)

    if not silent:
        handlers.append(stream_handler)

    logging.basicConfig(level=logging.INFO, handlers=handlers)
    start = datetime.now()

    if configpath:
        config.load(Path(configpath))

    if inputpath and not any(['build' in actions, 'all' in actions]):
        dset: CCDataset = CCDataset.from_json(Path(inputpath))
        # TODO: Contents of the old dataset should be copied into the new folder
        dset.root_dir = output
    elif inputpath and any(['build' in actions, 'all' in actions]):
        print(f'Warning: you wanted to build a dataset but you provided one in JSON -- that will be ignored. New one will be constructed at: {output}')

    actions = set(actions)
    if 'all' in actions:
        actions = {'build', 'download', 'convert', 'analyze'}

    if 'build' in actions:
        dset: CCDataset = CCDataset(certs={}, root_dir=output, name=f'CommonCriteria dataset', description=f'Full CommonCriteria dataset snapshot {datetime.now().date()}')
        dset.get_certs_from_web()
    elif 'build' not in actions and not inputpath:
        print('Error: If you do not provide input parameter, you must use \'build\' action to build dataset first.')
        sys.exit(1)

    if 'download' in actions:
        if not dset.state.meta_sources_parsed:
            logger.error('Attempt to download pdfs while cc web not parsed.')
            print('Error: You want to download all pdfs, but the data from commoncriteria.org was not parsed. You must use \'build\' action first.')
            sys.exit(1)
        dset.download_all_pdfs()

    if 'convert' in actions:
        if not dset.state.pdfs_downloaded:
            logger.error('Attempt to convert pdfs that were not downloaded.')
            print('Error: You want to convert pdfs -> txt, but the pdfs were not downloaded. You must use \'download\' action first.')
            sys.exit(1)
        dset.convert_all_pdfs()

    if 'analyze' in actions:
        if not dset.state.pdfs_converted:
            logger.error('Attempt to analyze certificates that were not downloaded.')
            print('Error: You want to process txt documents of certificates, but pdfs were not converted. You must use \'convert\' action first.')
            sys.exit(1)
        dset.extract_data()
        dset.compute_heuristics()

    end = datetime.now()
    logger.info(f'The computation took {(end-start)} seconds.')


if __name__ == '__main__':
    main()
