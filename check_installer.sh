#!/usr/bin/env bash
#
# About: Check the installer program with Docker locally
# WARN : The ComNetsEmu CAN NOT run inside Docker container, this script is
# just used to check the install, update functions of ./install.sh
#

# Fail on error
set -e

# Fail on unset var usage
set -o nounset

# TODO:  <06-06-19, Zuo> Test installation on other distributions
# BUG: debian:buster can not install OpenVirtex
# TEST_IMAGES=("ubuntu:18.04" "debian:jessie")

TEST_IMAGES=("ubuntu:18.04")
TEST_OPTIONS=("-l" "-o")
COMNETSEMU_DIR="/root/comnetsemu"

for img in "${TEST_IMAGES[@]}"; do
    for opt in "${TEST_OPTIONS[@]}"; do
        echo "*** Check the installation on $img with option $opt"

        sudo docker build -t "test_comnetsemu_install_$img" -f- . <<EOF
FROM $img

ENV COMNETSEMU_DIR=/root/comnetsemu

RUN apt-get update && apt-get install -y sudo git make apt-utils

    WORKDIR /root
RUN mkdir -p $COMNETSEMU_DIR/comnetsemu -p $COMNETSEMU_DIR/util
COPY ./comnetsemu/ $COMNETSEMU_DIR/comnetsemu
COPY ./Makefile $COMNETSEMU_DIR/Makefile
COPY ./util/ $COMNETSEMU_DIR/util
COPY ./setup.py $COMNETSEMU_DIR/setup.py
WORKDIR $COMNETSEMU_DIR/util

RUN bash ./install.sh $opt

CMD ["bash"]
EOF
        sudo docker image rm "test_comnetsemu_install_$img"
    done
done