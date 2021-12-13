#By: Lucas Zanchetta

import subprocess
import concurrent.futures
import paho.mqtt.client as paho

#Broker IP and port
broker="192.168.1.127"
port=1883

def powerDraw(ip):
    #Change to the location of your powerConsumption.sh script
    pcScript = subprocess.check_output(['/Users/lucas/Code/scripts/idrac/powerConsumption.sh', ip])
    pcScript=pcScript.decode('utf-8')
    pcScript=pcScript.split('\n')
    return float(pcScript[0])

#Add or remove iDrac IPs in this array
ips=["192.168.1.178", "192.168.1.120", "192.168.1.121"]

wattages = []
futures_list = []

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    for ip in ips:
        futures = executor.submit(powerDraw, ip)
        futures_list.append(futures)

    for future in futures_list:
        try:
            result = future.result(timeout=60)
            wattages.append(result)
        except Exception:
            wattages.append(None)

totalPower = 0
for i in range(len(wattages)):
    totalPower = totalPower + wattages[i]

def on_publish(client,userdata,result):
    print("data published \n")
    pass
client1= paho.Client("serverPower")
client1.on_publish = on_publish
client1.connect(broker,port)

#Add publish statements or remove them for more or less servers
ret= client1.publish("server1Power", wattages[0]) 
ret= client1.publish("server2Power", wattages[1]) 
ret= client1.publish("server3Power", wattages[2]) 
ret= client1.publish("serversPower", totalPower) 
