#!/bin/bash

ip=$1

pdRaw=$(ssh root@$ip racadm getconfig -g cfgServerPower -o cfgServerPowerLastMinAvg)

pd=$(cut -d ' ' -f 1 <<<${pdRaw})
echo $pd