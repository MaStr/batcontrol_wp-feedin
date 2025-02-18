import time
import json
import logging
import paho.mqtt.client as mqtt
import numpy as np
import yaml  # PyYAML muss installiert sein

SLEEP_INTERVALL_NO_SOLAR = 10
SLEEP_INTERVALL_CAR = 30

def convert_to_hourly_values(data):
    # Umsetzen in ein Array mit hourly values und dann in ein numpy array
    hourly_values = np.zeros(len(data['data']))
    ts = time.time()
    ts_full_hour = ts - (ts % 3600)

    hour = 0
    for i, entry in enumerate(data['data']):
        if entry['time_start'] < ts and entry['time_end'] > ts:
            # aktueller Wert
            hourly_values[0] = entry['value']
        else:
            # time_start von ts abziehen, durch 3600 und runden
            #logging.info(  (entry['time_start'] - ts_full_hour) / 3600 )
            hour = round( (entry['time_start'] - ts_full_hour) / 3600 )
            if hour < 0:
                continue
            hourly_values[hour] = entry['value']

    return hourly_values


class WP_EM_Adjustment:
    def __init__(self, config: dict):
        self.config = config
        self.client = mqtt.Client()
        self.last_delta_power = 0
        self.sleep_intervall = 0
        self.soc = 0  # Initialize soc attribute

        # Setze User und Passwort, wenn in der Config vorhanden
        mqtt_config = self.config.get('mqtt', {})
        user = mqtt_config.get('user')
        password = mqtt_config.get('password')
        if user and password:
            self.client.username_pw_set(user, password)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.connect(self.config['mqtt']['host'], self.config['mqtt']['port'], 60)
        self.client.loop_start()

        # Mappe alle Topics als Attribute
        self.topics = self.config['mqtt']['topics']
        for key, topic in self.topics.items():
            if key.startswith("set_"):
                # Erstelle einen Setter, der eine Nachricht pusht
                setattr(self, key, lambda message, topic=topic: self.client.publish(topic, message))
            else:
                # Anderen Topics als Attribut zuweisen (vorerst der Topic-String)
                setattr(self, key, topic)

    def on_connect(self, client, userdata, flags, rc):
        logging.info(f"Connected with result code {rc}")
        # Subscribe für alle Nicht-Setter-Topics
        for key, topic in self.topics.items():
            if not key.startswith("set_"):
                self.client.subscribe(topic)
                logging.info(f"Subscribed to topic: {topic}")

    def on_message(self, client, userdata, msg):
        decoded = msg.payload.decode()
        logging.debug(f"Received message on {msg.topic}: {decoded}")
        # Passe das Attribut an, das zum empfangenen Topic gehört
        for key, topic in self.topics.items():
            if not key.startswith("set_") and topic == msg.topic:
                setattr(self, key, decoded)
                logging.debug(f"Updated attribute '{key}' with value: {decoded}")
                if key == "grid_power":
                    self.evaluate()

    def x_set_em_mode(self, mode):
        if self.current_em_mode == mode:
            return
        self.client.publish(self.topics['set_em_mode'], mode)

    def x_set_em_power(self, power):
        power = int(round(power, 0))
        current_power = int(round(float(self.current_em_power), 0))
        if abs(power - current_power) <= abs(current_power * 0.15):
            logging.info("Power is already set to ~ %s ; %.3f", power, float(self.current_em_power))
            return
        logging.info("Set EM Power to %s", power)
        self.client.publish(self.topics['set_em_power'], power)
        self.last_delta_power = power

    def is_solar_ueberschuss_expected(self):
        fcst_solar = json.loads(self.batcontrol_fcst_solar)
        fcst_net_consumption = json.loads(self.batcontrol_fcst_net_consumption)
        # Umsetzen in ein Array mit hourly values und dann in ein numpy array
        net_consumption = convert_to_hourly_values(fcst_net_consumption)
        production = convert_to_hourly_values(fcst_solar)
        # aktuelle Stunde factorieren und um den Wert die Zelle null reduzieren:
        ts = time.time()
        ts_factor = 1 - (ts % 3600 / 3600)

        logging.debug("Net Consumption: %s", net_consumption)
        logging.debug("Production: %s", production)
        logging.debug("Ts Factor: %s", ts_factor)

        # Stunde null auf den fakturierten Wert setzen
        net_consumption[0] = net_consumption[0] * ts_factor
        production[0] = production[0] * ts_factor

        logging.debug("Net Consumption: %s", net_consumption)
        logging.debug("Production: %s", production)

        ## Erste Produktions-Stunde finden:
        production_start_time = 0
        production_end_time = None
        for i in range(len(production)):
            if production[i] > 0:
                production_start_time = i
                break

        if production_start_time is not None:
            for i in range(production_start_time, len(production)):
                if production[i] == 0:
                    production_end_time = i
                    break
            # Falls kein Ende gefunden wird, setze production_end_time ans Ende des Arrays
            if production_end_time is None:
                production_end_time = len(production)

        logging.debug("Production Start Time: %s", production_start_time)
        logging.debug("Production End Time: %s", production_end_time)

        ## Summiere die net_consumption im Zeitraum production_start_time bis production_end_time
        sum_net_consumption = 0
        for i in range(production_start_time, production_end_time+1):
            sum_net_consumption += net_consumption[i]
        sum_net_consumption = sum_net_consumption * -1
        if sum_net_consumption < 0:
            sum_net_consumption = 0
        logging.debug("Sum Net Consumption: %s", sum_net_consumption)
        # negativ bedeutet Überschuss, positiv bedeutet Bedarf

        # Zielwert ist 95% der Kapazität, da auf den letzten % die Ladeleistung abfällt
        free_capacity = (float(self.batcontrol_max_capacity) * 0.95) - float(self.batcontrol_stored_energy)
        logging.info("Freie Speicherkapazität (90%%) %.2f , netto Produktion %.2f", free_capacity, sum_net_consumption)
        if free_capacity < (sum_net_consumption * -1):
            return True
        return False

    def evaluate(self):
        if self.current_em_mode not in ("0", "1"):
            return

        if self.batcontrol_status == "offline":
            logging.info("Batcontrol offline")
            return

        if self.current_em_mode in ("0", "1"):
            if self.sleep_intervall > 0:
                self.sleep_intervall -= 1
                return
            if not (self.is_solar_ueberschuss_expected()):
                logging.info("Kein Solarüberschuss erwartet, Deaktiviere Steuerung")
                self.sleep_intervall = SLEEP_INTERVALL_NO_SOLAR
                if self.current_em_mode == "1":
                    self.x_set_em_mode(0)
                    self.x_set_em_power(0)
                return

        try:
            float(self.z1_zaehler)
        except (ValueError, TypeError):
            logging.error("z1_zaehler is not a valid number")
            return

        if self.batcontrol_mode == "0":
            logging.info("Batcontrol Mode 0, disable WP EM Adjustment")
            if self.current_em_mode == "1":
                self.x_set_em_mode(0)
                self.x_set_em_power(0)
            return

        if self.current_em_mode == "1":
            if self.ev_connected == "true":
                logging.info("EV just Connected, disable EM")
                self.x_set_em_mode(0)
                self.x_set_em_power(0)
                return

            if float(self.grid_power) > 100:
                self.x_set_em_power(0)
                return

            if float(self.soc) <= 50:
                self.x_set_em_mode(0)
                self.x_set_em_power(0)
                return

            delta_power = float(self.grid_power) - float(self.z1_zaehler)
            logging.info(f"Delta Power: {delta_power}")

            if delta_power > 100:
                self.x_set_em_power(0)
                return

            if float(self.pv_power) > 10:
                if float(self.soc) > 90:
                    nop = 0
                else:
                    delta_power = max(delta_power, -1 * (float(self.pv_power) - float(self.home_power)))

            self.z1_zaehler = 'NaN'
            self.x_set_em_power(delta_power)

        if self.current_em_mode == "0":
            if self.ev_connected == "true":
                logging.info("EV Connected, do nothing")
                self.sleep_intervall = SLEEP_INTERVALL_CAR
                return

            if float(self.soc) > 50:
                self.x_set_em_mode(1)
            else:
                logging.info("Warten bis SOC > 50%% aktuell: %s", self.soc)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    # Lese die Konfiguration aus config.yaml ein
    with open("config.yaml", "r") as config_file:
        config = yaml.safe_load(config_file)
    wp_em_adjustment = WP_EM_Adjustment(config)

    # Beispiel: Aufruf der Setter für 'set_em_power'
    # wp_em_adjustment.set_em_power("neuer Wert")

    while True:
        time.sleep(1)