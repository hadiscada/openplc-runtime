#!/bin/bash

gcc -I ./lib -c Config0.c -w -fPIC
gcc -I ./lib -c Res0.c -w -fPIC
gcc -I ./lib -c debug.c -w -fPIC
./xml2st --generate-gluevars LOCATED_VARIABLES.h
gcc -I ./lib -shared -o libplc.dylib Config0.o Res0.o debug.o glueVars.c -fPIC -w
mv ./libplc.dylib ../
