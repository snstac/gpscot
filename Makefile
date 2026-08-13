REPO_NAME ?= gpscot
PY_SRC = src/$(REPO_NAME)
SHELL := /bin/bash
.DEFAULT_GOAL := editable

prepare:
	mkdir -p build/

editable:
	python3 -m pip install -e .

install_test_requirements:
	python3 -m pip install -r requirements_test.txt

install:
	python3 setup.py install

uninstall:
	python3 -m pip uninstall -y $(REPO_NAME)

clean:
	@rm -rf *.egg* build dist *.py[oc] */*.py[co] cover doctest_pypi.cfg \
		nosetests.xml pylint.log output.xml flake8.log tests.log \
		test-result.xml htmlcov fab.log .coverage __pycache__ \
		*/__pycache__ deb_dist .mypy_cache

pep8:
	flake8 --max-line-length=88 --extend-ignore=E203 --exit-zero $(PY_SRC)/*.py

flake8: pep8

lint:
	pylint --msg-template="{path}:{line}: [{msg_id}({symbol}), {obj}] {msg}" \
		--max-line-length=88 -r n $(PY_SRC)/*.py || exit 0

pylint: lint

pytest:
	pytest

test: editable install_test_requirements pytest

test_cov:
	pytest --cov=$(REPO_NAME) --cov-report term-missing

deb_dist:
	python3 setup.py --command-packages=stdeb.command sdist_dsc

deb_custom:
	cp debian/$(REPO_NAME).conf $(wildcard deb_dist/*/debian)/$(REPO_NAME).default
	cp debian/$(REPO_NAME).postinst $(wildcard deb_dist/*/debian)/$(REPO_NAME).postinst
	cp debian/$(REPO_NAME).prerm $(wildcard deb_dist/*/debian)/$(REPO_NAME).prerm
	cp debian/$(REPO_NAME).service $(wildcard deb_dist/*/debian)/$(REPO_NAME).service

bdist_deb: deb_dist deb_custom
	cd deb_dist/$(REPO_NAME)-*/ && dpkg-buildpackage -rfakeroot -uc -us

faux_latest:
	cp deb_dist/$(REPO_NAME)_*-1_all.deb deb_dist/$(REPO_NAME)_latest_all.deb
	cp deb_dist/$(REPO_NAME)_*-1_all.deb deb_dist/python3-$(REPO_NAME)_latest_all.deb

package: bdist_deb faux_latest
