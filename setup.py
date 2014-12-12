# BEGIN_COPYRIGHT
#
# Copyright 2009-2014 CRS4.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# END_COPYRIGHT

"""
Important environment variables
-------------------------------

The Pydoop setup looks in a number of default paths for what it
needs.  If necessary, you can override its behaviour or provide an
alternative path by exporting the environment variables below::

  JAVA_HOME, e.g., /opt/sun-jdk
  HADOOP_HOME, e.g., /opt/hadoop-1.0.2

Other relevant environment variables include::

  HADOOP_VERSION, e.g., 0.20.2-cdh3u4 (override Hadoop's version string).
"""

import time
import os
import re
import glob
import shutil
import itertools
import subprocess

from distutils.core import setup, Extension
from distutils.command.build import build
from distutils.command.clean import clean
from distutils.errors import DistutilsSetupError, DistutilsOptionError
from distutils import log

import pydoop
import pydoop.utils.jvm as jvm
import pydoop.hadoop_utils as hu
import pydoop.hdfs.core.impl as hdfsimpl


JAVA_HOME = jvm.get_java_home()
JVM_LIB_PATH, JVM_LIB_NAME = jvm.get_jvm_lib_path_and_name(JAVA_HOME)

HADOOP_HOME = pydoop.hadoop_home(fallback=None)
HADOOP_VERSION_INFO = pydoop.hadoop_version_info()

EXTENSION_MODULES = []


# ---------
# UTILITIES
# ---------

def rm_rf(path, dry_run=False):
    """
    Remove a file or directory tree.

    Won't throw an exception, even if the removal fails.
    """
    log.info("removing %s" % path)
    if dry_run:
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError:
        pass


def mtime(fn):
    return os.stat(fn).st_mtime


def must_generate(target, prerequisites):
    try:
        return max(mtime(p) for p in prerequisites) > mtime(target)
    except OSError:
        return True


def get_version_string(filename="VERSION"):
    try:
        with open(filename) as f:
            return f.read().strip()
    except IOError:
        raise DistutilsSetupError("failed to read version info")


def write_config(filename="pydoop/config.py", hdfs_core_impl=hdfsimpl.DEFAULT):
    prereq = "DEFAULT_HADOOP_HOME"
    if not os.path.exists(prereq):
        with open(prereq, "w") as f:
            f.write("%s\n" % HADOOP_HOME)
    # if must_generate(filename, [prereq]):
    with open(filename, "w") as f:
        f.write("# GENERATED BY setup.py\n")
        f.write("DEFAULT_HADOOP_HOME='%s'\n" % HADOOP_HOME)
        f.write("HDFS_CORE_IMPL='%s'\n" % hdfs_core_impl)


def generate_hdfs_config():
    """
    Generate config.h for libhdfs.

    This is only relevant for recent Hadoop versions.
    """
    config_fn = os.path.join(
        'src', 'libhdfs', str(HADOOP_VERSION_INFO), "config.h"
    )
    with open(config_fn, "w") as f:
        f.write("#ifndef CONFIG_H\n#define CONFIG_H\n")
        if have_better_tls():
            f.write("#define HAVE_BETTER_TLS\n")
        f.write("#endif\n")


def get_git_commit():
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD']).rstrip('\n')
    except subprocess.CalledProcessError:
        return None


def write_version(filename="pydoop/version.py"):
    prereq = "VERSION"
    if must_generate(filename, [prereq]):
        version = get_version_string(filename=prereq)
        git_commit = get_git_commit()
        with open(filename, "w") as f:
            f.write("# GENERATED BY setup.py\n")
            f.write("version='%s%s'\n" % (
                version, '' if git_commit is None else (' [%s]' % git_commit))
            )


def build_hdfscore_native_impl():
    generate_hdfs_config()
    hdfs_ext_sources = []
    hadoop_v = HADOOP_VERSION_INFO.tuple[0]
    hdfs_ext_sources += [os.path.join('src/libhdfs',
                                      str(HADOOP_VERSION_INFO), x)
                         for x in (['hdfs.c', 'hdfsJniHelper.c']
                                   if hadoop_v <= 1 else
                                   ['hdfs.c', 'jni_helper.c', 'exception.c',
                                    'native_mini_dfs.c'])]
    hdfs_ext_sources += [
        os.path.join('src/native_core_hdfs', x) for x in [
            'hdfs_module.cc', 'hdfs_file.cc', 'hdfs_fs.cc'
        ]]
    libhdfs_macros = [("HADOOP_LIBHDFS_V1" if hadoop_v <= 1
                       else "HADOOP_LIBHDFS_V2", 1)]
    native_hdfs_core = Extension(
        'native_core_hdfs',
        include_dirs=jvm.get_include_dirs() + [
            os.path.join('src/libhdfs', str(HADOOP_VERSION_INFO))
        ],
        libraries=jvm.get_libraries(),
        library_dirs=[JAVA_HOME + "/Libraries", JVM_LIB_PATH],
        sources=hdfs_ext_sources,
        define_macros=jvm.get_macros() + libhdfs_macros,
        extra_compile_args=['-Xlinker', '-rpath', JVM_LIB_PATH]
    )
    EXTENSION_MODULES.append(native_hdfs_core)


def build_sercore_extension():
    binary_encoder = Extension(
        'pydoop_sercore',
        sources=[os.path.join('src/serialize', x) for x in [
            'protocol_codec.cc', 'SerialUtils.cc', 'StringUtils.cc'
        ]],
        extra_compile_args=["-O3"]
    )
    EXTENSION_MODULES.append(binary_encoder)


def have_better_tls():
    """
    See ${HADOOP_HOME}/hadoop-hdfs-project/hadoop-hdfs/src/CMakeLists.txt
    """
    return False  # FIXME: need a portable implementation


# ------------
# BUILD ENGINE
# ------------

class JavaLib(object):

    def __init__(self, hadoop_vinfo):
        self.hadoop_vinfo = hadoop_vinfo
        self.jar_name = pydoop.jar_name(self.hadoop_vinfo)
        self.classpath = pydoop.hadoop_classpath()
        self.java_files = [
            "src/it/crs4/pydoop/NoSeparatorTextOutputFormat.java"
        ]
        self.java_files.extend(glob.glob(
            'src/it/crs4/pydoop/pipes/*.java'
        ))
        if hadoop_vinfo.main >= (2, 2, 0):
            self.java_files.extend(glob.glob(
                'src/it/crs4/pydoop/mapreduce/pipes/*.java'
            ))

class JavaBuilder(object):

    def __init__(self, build_temp, build_lib):
        self.build_temp = build_temp
        self.build_lib = build_lib
        self.java_libs = [JavaLib(HADOOP_VERSION_INFO)]

    def run(self):
        log.info("hadoop_home: %r" % (HADOOP_HOME,))
        log.info("hadoop_version: '%s'" % HADOOP_VERSION_INFO)
        log.info("java_home: %r" % (JAVA_HOME,))
        for jlib in self.java_libs:
            self.__build_java_lib(jlib)

    def __build_java_lib(self, jlib):
        log.info("Building java code for hadoop-%s" % jlib.hadoop_vinfo)
        compile_cmd = "javac"
        if jlib.classpath:
            compile_cmd += " -classpath %s" % jlib.classpath
        else:
            log.warn(
                "WARNING: could not set classpath, java code may not compile"
            )
        class_dir = os.path.join(
            self.build_temp, "pipes-%s" % jlib.hadoop_vinfo
        )
        package_path = os.path.join(self.build_lib, "pydoop", jlib.jar_name)
        if not os.path.exists(class_dir):
            os.mkdir(class_dir)
        compile_cmd += " -d '%s'" % class_dir
        log.info("Compiling Java classes")
        for f in jlib.java_files:
            compile_cmd += " %s" % f
        ret = os.system(compile_cmd)
        if ret:
            raise DistutilsSetupError(
                "Error compiling java component.  Command: %s" % compile_cmd
            )
        log.info("Making Jar: %s", package_path)
        package_cmd = "jar -cf %(package_path)s -C %(class_dir)s ./it" % {
            'package_path': package_path, 'class_dir': class_dir
        }
        log.info("Packaging Java classes")
        log.info("Command: %s", package_cmd)
        ret = os.system(package_cmd)
        if ret:
            raise DistutilsSetupError(
                "Error packaging java component.  Command: %s" % package_cmd
            )


class BuildPydoop(build):

    user_options = build.user_options
    user_options.append((
        'hdfs-core-impl=', None,
        "hdfs core implementation [%s]" % ", ".join(hdfsimpl.SUPPORTED)
    ))

    def __init__(self, dist):
        build.__init__(self, dist)
        self.hdfs_core_impl = hdfsimpl.DEFAULT

    def finalize_options(self):
        build.finalize_options(self)
        if self.hdfs_core_impl not in hdfsimpl.SUPPORTED:
            raise DistutilsOptionError(
                '%r not supported' % (self.hdfs_core_impl,)
            )

    def build_java(self):
        jb = JavaBuilder(self.build_temp, self.build_lib)
        jb.run()

    def create_tmp(self):
        if not os.path.exists(self.build_temp):
            os.mkdir(self.build_temp)
        if not os.path.exists(self.build_lib):
            os.mkdir(self.build_lib)

    def clean_up(self):
        shutil.rmtree(self.build_temp)

    def run(self):
        print "hdfs core implementation: {0}".format(self.hdfs_core_impl)
        write_config(hdfs_core_impl=self.hdfs_core_impl)
        write_version()
        build_sercore_extension()
        if self.hdfs_core_impl == hdfsimpl.NATIVE:
            build_hdfscore_native_impl()
        build.run(self)
        try:
            self.create_tmp()
            self.build_java()
        finally:
            # On NFS, if we clean up right away we have issues with
            # NFS handles being still in the directory trees to be
            # deleted.  So, we sleep a bit and then delete
            time.sleep(0.5)
            self.clean_up()
        log.info("Build finished")


class Clean(clean):

    def run(self):
        clean.run(self)
        garbage_list = [
            "DEFAULT_HADOOP_HOME",
            "pydoop/config.py",
            "pydoop/version.py",
        ]
        garbage_list.extend(glob.iglob("build"))
        garbage_list.extend(glob.iglob("src/*.patched"))
        garbage_list.extend(p for p in itertools.chain(
            glob.iglob('src/*'), glob.iglob('patches/*')
        ) if os.path.islink(p))
        for p in garbage_list:
            rm_rf(p, self.dry_run)


setup(
    name="pydoop",
    version=get_version_string(),
    description=pydoop.__doc__.strip().splitlines()[0],
    long_description=pydoop.__doc__.lstrip(),
    author=pydoop.__author__,
    author_email=pydoop.__author_email__,
    url=pydoop.__url__,
    download_url="https://sourceforge.net/projects/pydoop/files/",
    packages=[
        "pydoop",
        "pydoop.hdfs",
        "pydoop.hdfs.core",
        "pydoop.hdfs.core.bridged",
        "pydoop.app",
        "pydoop.mapreduce",
        "pydoop.utils",
        "pydoop.utils.bridge",
    ],
    cmdclass={
        "build": BuildPydoop,
        "clean": Clean
    },
    scripts=["scripts/pydoop"],
    platforms=["Linux"],
    ext_modules=EXTENSION_MODULES,
    license="Apache-2.0",
    keywords=["hadoop", "mapreduce"],
    classifiers=[
        "Programming Language :: Python",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: POSIX :: Linux",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Intended Audience :: Developers",
    ],
    data_files=[
        ('config', ['README.md']),
    ],
)
