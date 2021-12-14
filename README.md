# iDracPowerMonitorMQTT

This script publishes iDrac power draw data for iDrac 6 enabled servers to an MQTT broker.
This can be used to integrate the power draw statistics into something like HomeAssistant for your home lab among many other uses.

**Set Up:**

1. This script uses the paho-mqtt python3 library, to install this run `pip3 install paho-mqtt`
2. Follow instructions in the python file to configure the script to connect to your MQTT broker and add your iDracs
3. This script also assumes you are using key based ssh authentication to access your iDracs, to copy your keys over, use the racadm command or the iDrac web interface
4. This script can be run via a cronjob to update the statistics at a set interval
