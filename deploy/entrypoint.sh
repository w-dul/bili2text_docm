#!/bin/sh
set -e

bili2text server --host 127.0.0.1 --port 8000 &
exec nginx -g 'daemon off;'
