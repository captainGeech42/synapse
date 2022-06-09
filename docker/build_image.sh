#!/usr/bin/env bash

##############################################################################
#
# Build and tag a specific synapse image.
#
# This is expected to be executed from the root of the repository; eg:
#
# ./docker/build_image.sh cortex
#
# The first argument is the server (in docker/images) to build.
# A second argument may be provided, including the tag to build.
# A default tag will be used if one is not provided.
#
# This will build a base image, if it does not exist, using the
# ./docker/build_base.sh script.
#
##############################################################################

set -e # exit on nonzero
set -u # undefined variables
set -o pipefail # pipefail propagate error codes
# set -x # debugging

BASEIMAGE=synbuild:base

IMAGE=${1:-}
if [ ${IMAGE} == "synapse" ]
then
    echo "The synapse image is build with build_base.sh"
    false
fi
IMAGE_DIR=docker/images/${IMAGE}
[ ! -d $IMAGE_DIR ] && echo "$IMAGE_DIR does not exist." && false

TAG=${2:-}

[ ! $TAG ] && echo "Tag not provided, defaulting tag to dev_build" && TAG=dev_build

# Chuck the syndev:base image if it exists.
SYNDEVBASE_EXISTS=`docker image ls -q ${BASEIMAGE} | wc -l`
if [ ${SYNDEVBASE_EXISTS} == "0" ]
then
    docker/build_base.sh
else
    echo "Using existing ${BASEIMAGE}. Use docker/build_base.sh if you need a updated base image".
fi

# Build target image
echo "Building from docker/images/$IMAGE/Dockerfile"
docker build -t vertexproject/synapse-$IMAGE:$TAG -f docker/images/$IMAGE/Dockerfile .
