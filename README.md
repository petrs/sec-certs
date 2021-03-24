![](sec_certs/static/img/logo.svg)

# seccerts.org page

This branch contains a [Flask](https://palletsprojects.com/p/flask/) app that is available
at [seccerts.org](https://seccerts.org) and can be used to serve a page with the data locally.

## Usage

1. Install the requirements, it is recommended to do so into a newly created Python virtual environment.
   The minimal required Python version is 3.8.
   ```shell
   python -m venv virt                # Creates the virtualenv.
   . virt/bin/activate                # Activates it.
   pip install -r requirements.txt    # Installs the requirements
   ```
2. Create the `instance` directory.
   ```shell
   mkdir instance 
   ```
3. Import the data generated by the main seccerts tool into the `instance` directory:
   ```shell
   cp certificate_data_complete_processed.json instance/cc.json
   cp fips_full_dataset.json instance/fips.json
   cp pp_data_complete_processed.json instance/pp.json
   ```
4. Create a `config.py` file in the `instance` directory:
   ```python
   # A Flask SECRET_KEY used for sensitive operations (like signing session cookies),
   # needs to be properly random.
   # For example the output of "openssl rand -hex 32" or "python -c 'import os; print(os.urandom(16))'"
   SECRET_KEY = "some proper randomness here"

   # The way the Common Criteria certificate reference graphs are built.
   # Can be "BOTH" to collect the references from both certificate documents and security targets,
   # or "CERT_ONLY" for collecting references from certs only,
   # or "ST_ONLY" for collecting references from security targets only.
   CC_GRAPH = "CERT_ONLY"

   # Number of items per page in the search listing.
   SEARCH_ITEMS_PER_PAGE = 20
   ```
5. Run the Flask app. The first request to the app will take a long time, as the app
   lazily loads the instance resources and does some processing.
   ```shell
   env FLASK_APP=sec_certs FLASK_ENV=production flask run
   ```