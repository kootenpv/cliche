from setuptools import find_packages
from setuptools import setup

MAJOR_VERSION = "0"
MINOR_VERSION = "10"
MICRO_VERSION = "108"
VERSION = "{}.{}.{}".format(MAJOR_VERSION, MINOR_VERSION, MICRO_VERSION)

setup(
    name="cliche",
    version=VERSION,
    description="A minimalistic CLI wrapper out to be the best",
    url="https://github.com/kootenpv/cliche",
    author="Pascal van Kooten",
    author_email="kootenpv@gmail.com",
    entry_points={"console_scripts": ["cliche = cliche.__init__:main"]},
    license="MIT",
    packages=find_packages(),
    include_package_data=True,
    install_requires=["ipdb == 0.13.9"],
    classifiers=[
        "Environment :: Console",
        "Intended Audience :: Developers",
        "Intended Audience :: Customer Service",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: Microsoft",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Unix",
        "Operating System :: POSIX",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.4",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Software Development",
        "Topic :: Software Development :: Build Tools",
        "Topic :: Software Development :: Debuggers",
        "Topic :: Software Development :: Libraries",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Software Distribution",
        "Topic :: System :: Systems Administration",
        "Topic :: Utilities",
    ],
    zip_safe=False,
    platforms="any",
)
