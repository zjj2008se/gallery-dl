# -*- coding: utf-8 -*-

# Copyright 2015-2018 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

import sys
import time
import json
import hashlib
import logging
from . import extractor, downloader, config, util, output, exception
from .extractor.message import Message


class Job():
    """Base class for Job-types"""
    ufile = None

    def __init__(self, url, parent=None):
        self.url = url
        self.extractor = extractor.find(url)
        if self.extractor is None:
            raise exception.NoExtractorError(url)
        self.extractor.log.debug(
            "Using %s for '%s'", self.extractor.__class__.__name__, url)

        # url predicates
        predicates = [util.UniquePredicate()]
        image = config.get(("_", "image"), {})
        if "filter" in image:
            predicates.append(util.FilterPredicate(image["filter"]))
        if "range" in image:
            pred = util.RangePredicate(image["range"])
            if pred.lower > 1 and "filter" not in image:
                pred.index += self.extractor.skip(pred.lower - 1)
            predicates.append(pred)
        self.pred_url = util.build_predicate(predicates)

        # queue predicates
        predicates = []
        chapter = config.get(("_", "chapter"), {})
        if "filter" in chapter:
            predicates.append(util.FilterPredicate(chapter["filter"]))
        if "range" in chapter:
            predicates.append(util.RangePredicate(chapter["range"]))
        self.pred_queue = util.build_predicate(predicates)

        # category transfer
        if parent and parent.extractor.categorytransfer:
            self.extractor.category = parent.extractor.category
            self.extractor.subcategory = parent.extractor.subcategory

        # user-supplied metadata
        self.userkwds = self.extractor.config("keywords")

    def run(self):
        """Execute or run the job"""
        try:
            log = self.extractor.log
            for msg in self.extractor:
                self.dispatch(msg)
        except exception.AuthenticationError:
            log.error("Authentication failed. Please provide a valid "
                      "username/password pair.")
        except exception.AuthorizationError:
            log.error("You do not have permission to access the resource "
                      "at '%s'", self.url)
        except exception.NotFoundError as exc:
            res = str(exc) or "resource (gallery/image/user)"
            log.error("The %s at '%s' does not exist", res, self.url)
        except exception.HttpError as exc:
            log.error("HTTP request failed:  %s", exc)
        except exception.FormatError as exc:
            err, obj = exc.args
            log.error("Applying %s format string failed:  %s: %s",
                      obj, err.__class__.__name__, err)
        except exception.FilterError as exc:
            err = exc.args[0]
            log.error("Evaluating filter expression failed:  %s: %s",
                      err.__class__.__name__, err)
        except exception.StopExtraction:
            pass
        except OSError as exc:
            log.error("Unable to download data: %s", exc)
        except Exception as exc:
            log.error(("An unexpected error occurred: %s - %s. "
                       "Please run gallery-dl again with the --verbose flag, "
                       "copy its output and report this issue on "
                       "https://github.com/mikf/gallery-dl/issues ."),
                      exc.__class__.__name__, exc)
            log.debug("Traceback", exc_info=True)

    def dispatch(self, msg):
        """Call the appropriate message handler"""
        if msg[0] == Message.Url:
            _, url, kwds = msg
            if self.pred_url(url, kwds):
                self.update_kwdict(kwds)
                self.handle_url(url, kwds)

        elif msg[0] == Message.Directory:
            self.update_kwdict(msg[1])
            self.handle_directory(msg[1])

        elif msg[0] == Message.Queue:
            _, url, kwds = msg
            if self.pred_queue(url, kwds):
                self.handle_queue(url, kwds)

        elif msg[0] == Message.Urllist:
            _, urls, kwds = msg
            if self.pred_url(urls[0], kwds):
                self.update_kwdict(kwds)
                self.handle_urllist(urls, kwds)

        elif msg[0] == Message.Version:
            if msg[1] != 1:
                raise "unsupported message-version ({}, {})".format(
                    self.extractor.category, msg[1]
                )
            # TODO: support for multiple message versions

    def handle_url(self, url, keywords):
        """Handle Message.Url"""

    def handle_urllist(self, urls, keywords):
        """Handle Message.Urllist"""
        self.handle_url(urls[0], keywords)

    def handle_directory(self, keywords):
        """Handle Message.Directory"""

    def handle_queue(self, url, keywords):
        """Handle Message.Queue"""

    def update_kwdict(self, kwdict):
        """Update 'kwdict' with additional metadata"""
        kwdict["category"] = self.extractor.category
        kwdict["subcategory"] = self.extractor.subcategory
        if self.userkwds:
            kwdict.update(self.userkwds)

    def _write_unsupported(self, url):
        if self.ufile:
            print(url, file=self.ufile, flush=True)


class DownloadJob(Job):
    """Download images into appropriate directory/filename locations"""

    def __init__(self, url, parent=None):
        Job.__init__(self, url, parent)
        self.log = logging.getLogger("download")
        self.pathfmt = None
        self.archive = None
        self.sleep = None
        self.downloaders = {}
        self.out = output.select()

    def handle_url(self, url, keywords, fallback=None):
        """Download the resource specified in 'url'"""
        # prepare download
        self.pathfmt.set_keywords(keywords)

        if self.pathfmt.exists(self.archive):
            self.out.skip(self.pathfmt.path)
            return

        if self.sleep:
            time.sleep(self.sleep)

        # download from URL
        if not self.get_downloader(url).download(url, self.pathfmt):

            # use fallback URLs if available
            for num, url in enumerate(fallback or (), 1):
                self.log.info("Trying fallback URL #%d", num)
                if self.get_downloader(url).download(url, self.pathfmt):
                    break
            else:
                # download failed
                self.log.error(
                    "Failed to download %s", self.pathfmt.filename)
                return

        # download succeeded
        if self.archive:
            self.archive.add(keywords)

    def handle_urllist(self, urls, keywords):
        """Download the resource specified in 'url'"""
        fallback = iter(urls)
        url = next(fallback)
        self.handle_url(url, keywords, fallback)

    def handle_directory(self, keywords):
        """Set and create the target directory for downloads"""
        if not self.pathfmt:
            self.pathfmt = util.PathFormat(self.extractor)
            self.sleep = self.extractor.config("sleep")
            archive = self.extractor.config("archive")
            if archive:
                path = util.expand_path(archive)
                self.archive = util.DownloadArchive(path, self.extractor)
        self.pathfmt.set_directory(keywords)

    def handle_queue(self, url, keywords):
        try:
            DownloadJob(url, self).run()
        except exception.NoExtractorError:
            self._write_unsupported(url)

    def get_downloader(self, url):
        """Return, and possibly construct, a downloader suitable for 'url'"""
        pos = url.find(":")
        scheme = url[:pos] if pos != -1 else "http"
        if scheme == "https":
            scheme = "http"
        instance = self.downloaders.get(scheme)
        if instance is None:
            klass = downloader.find(scheme)
            instance = klass(self.extractor, self.out)
            self.downloaders[scheme] = instance
        return instance


class KeywordJob(Job):
    """Print available keywords"""

    def handle_url(self, url, keywords):
        print("\nKeywords for filenames and --filter:")
        print("------------------------------------")
        self.print_keywords(keywords)
        raise exception.StopExtraction()

    def handle_directory(self, keywords):
        print("Keywords for directory names:")
        print("-----------------------------")
        self.print_keywords(keywords)

    def handle_queue(self, url, keywords):
        if not keywords:
            self.extractor.log.info(
                "This extractor delegates work to other extractors "
                "and does not provide any keywords on its own. Try "
                "'gallery-dl -K \"%s\"' instead.", url)
        else:
            print("Keywords for --chapter-filter:")
            print("------------------------------")
            self.print_keywords(keywords)
            if self.extractor.categorytransfer:
                print()
                KeywordJob(url, self).run()
        raise exception.StopExtraction()

    @staticmethod
    def print_keywords(keywords, prefix=""):
        """Print key-value pairs with formatting"""
        suffix = "]" if prefix else ""
        for key, value in sorted(keywords.items()):
            key = prefix + key + suffix

            if isinstance(value, dict):
                KeywordJob.print_keywords(value, key + "[")

            elif isinstance(value, list):
                if value and isinstance(value[0], dict):
                    KeywordJob.print_keywords(value[0], key + "[][")
                else:
                    print(key, "[]", sep="")
                    for val in value:
                        print("  -", val)

            else:
                # string or number
                print(key, "\n  ", value, sep="")


class UrlJob(Job):
    """Print download urls"""
    maxdepth = 1

    def __init__(self, url, parent=None, depth=1):
        Job.__init__(self, url, parent)
        self.depth = depth
        if depth >= self.maxdepth:
            self.handle_queue = self.handle_url

    @staticmethod
    def handle_url(url, _):
        print(url)

    @staticmethod
    def handle_urllist(urls, _):
        prefix = ""
        for url in urls:
            print(prefix, url, sep="")
            prefix = "| "

    def handle_queue(self, url, _):
        try:
            UrlJob(url, self, self.depth + 1).run()
        except exception.NoExtractorError:
            self._write_unsupported(url)


class TestJob(DownloadJob):
    """Generate test-results for extractor runs"""

    class HashIO():
        """Minimal file-like interface"""

        def __init__(self, hashobj):
            self.hashobj = hashobj
            self.path = ""
            self.size = 0
            self.has_extension = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def open(self, mode):
            self.size = 0
            return self

        def write(self, content):
            """Update SHA1 hash"""
            self.size += len(content)
            self.hashobj.update(content)

        def tell(self):
            return self.size

        def part_size(self):
            return 0

    def __init__(self, url, parent=None, content=False):
        DownloadJob.__init__(self, url, parent)
        self.content = content
        self.list_url = []
        self.list_keyword = []
        self.list_archive = []
        self.hash_url = hashlib.sha1()
        self.hash_keyword = hashlib.sha1()
        self.hash_archive = hashlib.sha1()
        self.hash_content = hashlib.sha1()
        if content:
            self.fileobj = self.HashIO(self.hash_content)
            self.get_downloader("http:")._check_extension = lambda a, b: None

    def run(self):
        for msg in self.extractor:
            self.dispatch(msg)

    def handle_url(self, url, keywords):
        self.update_url(url)
        self.update_keyword(keywords)
        self.update_archive(keywords)
        self.update_content(url)

    def handle_urllist(self, urls, keywords):
        self.handle_url(urls[0], keywords)

    def handle_directory(self, keywords):
        self.update_keyword(keywords, False)

    def handle_queue(self, url, keywords):
        self.update_url(url)
        self.update_keyword(keywords)

    def update_url(self, url):
        """Update the URL hash"""
        self.list_url.append(url)
        self.hash_url.update(url.encode())

    def update_keyword(self, kwdict, to_list=True):
        """Update the keyword hash"""
        if to_list:
            self.list_keyword.append(kwdict.copy())
        self.hash_keyword.update(
            json.dumps(kwdict, sort_keys=True).encode())

    def update_archive(self, kwdict):
        """Update the archive-id hash"""
        archive_id = self.extractor.archive_fmt.format_map(kwdict)
        self.list_archive.append(archive_id)
        self.hash_archive.update(archive_id.encode())

    def update_content(self, url):
        """Update the content hash"""
        if self.content:
            self.get_downloader(url).download(url, self.fileobj)


class DataJob(Job):
    """Collect extractor results and dump them"""

    def __init__(self, url, parent=None, file=sys.stdout):
        Job.__init__(self, url, parent)
        self.file = file
        self.data = []
        self.ensure_ascii = config.get(("output", "ascii"), True)

    def run(self):
        # collect data
        try:
            for msg in self.extractor:
                self.dispatch(msg)
        except Exception as exc:
            self.data.append((exc.__class__.__name__, str(exc)))
        except BaseException:
            pass

        # dump to 'file'
        json.dump(
            self.data, self.file,
            sort_keys=True, indent=2, ensure_ascii=self.ensure_ascii
        )
        self.file.write("\n")

    def handle_url(self, url, keywords):
        self.data.append((Message.Url, url, keywords.copy()))

    def handle_urllist(self, urls, keywords):
        self.data.append((Message.Urllist, list(urls), keywords.copy()))

    def handle_directory(self, keywords):
        self.data.append((Message.Directory, keywords.copy()))

    def handle_queue(self, url, keywords):
        self.data.append((Message.Queue, url, keywords.copy()))
