import time
import json
import logging
from dataclasses import dataclass
import paho.mqtt.client as mqtt
import numpy as np
import argparse
import yaml  # PyYAML muss installiert sein


@dataclass
class EnergyManagementConstants:
    # Warteintervall, wenn kein Solarüberschuss erwartet wird
    sleep_interval_no_solar: int = 10
    # Warteintervall, wenn ein EV verbunden ist
    sleep_interval_car: int = 30
    # Mindest-SOC zum Starten der Regelung
    soc_threshold: int = 50
    # Oberer SOC-Schwellenwert, unter diesem Wert wird auf PV-Überschuss geregelt,
    #    darüber wird ungeregelt der Leistungsbedarf genutzt.
    high_soc_threshold: int = 90
    # Schwellenwert für Netzbezug um die Regelung zu deaktivieren
    #    z.B. Herd wird angemacht.
    grid_power_threshold: int = 100
    # Toleranz bei Power-Änderungen
    #    Wie fein soll der EM_Power nachgeregelt werden.
    power_tolerance_percent: float = 0.15
    # Factor für die verwendete Speicherkapazität
    #    Für die PV-Gesamt-Überschussberechnung wird die Akku Kapazität um diesen Wert verrringert.
    capacity_utilization: float = 0.95
    # Schwellenwert für anliegende PV-Leistung
    #    Fällt die PV Leistung unter diesem Wert, wird die Regelung abgeschaltet.
    pv_power_threshold: int = 10
    # Maximale zulässige positive Differenz (delta_power)
    #    Dieser Wert würde bedeuten, dass wir 100W mehr beziehen, statt einzuseisen.
    #    Wird dieser Wert überschritten, wird die Regelung abgeschaltet.
    delta_power_difference_max: int = 100
    # Maximale Leistung zum Einspeisen:
    power_feed_in_max: int = 3500



def convert_to_hourly_values(data, ts):
    # Umsetzen in ein Array mit hourly values und dann in ein numpy array
    hourly_values = np.zeros(len(data['data']))
    ts_full_hour = ts - (ts % 3600)

    for i, entry in enumerate(data['data']):
        if entry['time_start'] < ts and entry['time_end'] > ts:
            # aktueller Wert
            hourly_values[0] = entry['value']
        else:
            # time_start von ts abziehen, durch 3600 und runden
            hour = round((entry['time_start'] - ts_full_hour) / 3600)
            if hour < 0:
                continue
            hourly_values[hour] = entry['value']
    return hourly_values


class WP_EM_Adjustment:
    def __init__(self, config: dict):
        # Definiere hier, welche Typkonvertierung für welchen Topic-Schlüssel genutzt werden soll
        self.topic_conversions = {
            "z1_zaehler": float,
            "current_em_mode": lambda x: x,
            "current_em_power": float,
            "batcontrol_mode": lambda x: x,
            "batcontrol_status": lambda x: x,
            "soc": float,
            "grid_power": float,
            "ev_connected": lambda x: x,
            "pv_power": float,
            "home_power": float,
            "batcontrol_max_capacity": float,
            "batcontrol_stored_energy": float,
            # Diese Werte sind JSON-Strings, die später in Funktionen konvertiert werden
            "batcontrol_fcst_solar": lambda x: x,
            "batcontrol_fcst_net_consumption": lambda x: x,
        }
        self.config = config
        # Dry run Flag: wenn True, werden Publish-Aufrufe nur geloggt
        self.dry_run = self.config.get('dry_run', False)
        self.client = mqtt.Client()
        self.last_delta_power = 0
        self.sleep_interval = 0

        # Initialisiere bekannte Attribute, falls noch nicht gesetzt
        self.soc = 0
        self.current_em_mode = None
        self.current_em_power = 0
        self.z1_zaehler = 0
        self.batcontrol_mode = None
        self.batcontrol_status = None
        self.grid_power = 0
        self.ev_connected = "false"
        self.ev_charge_mode = "off"
        self.pv_power = 0
        self.home_power = 0
        self.batcontrol_max_capacity = 0
        self.batcontrol_stored_energy = 0
        self.batcontrol_fcst_solar = "[]"
        self.batcontrol_fcst_net_consumption = "[]"
        self.received_fcst = False
        self.z1_refreshed = False

        # Lade die EnergyManagement-Konstanten aus der Config (falls vorhanden)
        em_consts = self.config.get('em_constants', {})
        self.em_config = EnergyManagementConstants(**em_consts)

        self.__set_feedin_max( self.em_config.power_feed_in_max * -1 , "Initial")

        # Setze User und Passwort, falls in der Config vorhanden
        mqtt_config = self.config.get('mqtt', {})
        user = mqtt_config.get('user')
        password = mqtt_config.get('password')
        if user and password:
            self.client.username_pw_set(user, password)

        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        mqtt_host = self.config['mqtt'].get('host')
        mqtt_port = self.config['mqtt'].get('port')
        if not mqtt_host or not mqtt_port:
            raise ValueError(
                "MQTT host and port must be specified in the configuration")
        self.client.connect(mqtt_host, mqtt_port, 60)
        self.client.loop_start()

        # Mappe alle Topics als Attribute
        self.topics = self.config['mqtt']['topics']
        for key, topic in self.topics.items():
            # Falls noch kein Default-Wert gesetzt wurde, setzte den Topic-String
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
        # Aktualisiere das passende Attribut
        for key, topic in self.topics.items():
            if not key.startswith("set_") and topic == msg.topic:
                convert_func = self.topic_conversions.get(key, lambda x: x)
                try:
                    converted = convert_func(decoded)
                except Exception as e:
                    logging.error(f"Fehler bei der Typkonvertierung für {key} mit Wert '{decoded}': {e}")
                    converted = decoded  # Fallback: unverändert
                if key == "batcontrol_status":
                    if self.batcontrol_status == "offline":
                        if converted == "online":
                            logging.info("Batcontrol online")
                setattr(self, key, converted)
                logging.debug(f"Updated attribute '{key}' with value: {converted}")
                if key == "z1_zaehler":
                    self.z1_refreshed = True
                if key == "grid_power":
                    self.evaluate()
                if key.startswith("batcontrol_fcst_"):
                    self.received_fcst = True

    def stop(self):
        self.__disable_em()
        self.client.loop_stop()
        self.client.disconnect()

    def update_em_mode(self, mode):
        if self.current_em_mode == mode:
            return
        if self.dry_run:
            logging.info(
                f"Dry run: update_em_mode would publish {mode} to {self.topics.get('set_em_mode')}")
        else:
            self.client.publish(self.topics['set_em_mode'], mode)

    def update_em_power(self, power):
        """
        Set the energy management power to the specified value.

        Args:
            power (int): The power value to set.
        """
        power = int(round(power, 0))
        current_power = int(round(self.current_em_power, 0))
        if abs(power - current_power) <= abs(current_power * self.em_config.power_tolerance_percent):
            logging.info("Power is already set to ~ %s ; %.3f",
                         power, float(self.current_em_power))
            return
        logging.info("Set EM Power to %s", power)
        if self.dry_run:
            logging.info(
                f"Dry run: update_em_power would publish {power} to {self.topics.get('set_em_power')}")
        else:
            self.client.publish(self.topics['set_em_power'], power)
        self.last_delta_power = power

    def is_solar_ueberschuss_expected(self):
        ts = time.time()
        ts_factor = 1 - (ts % 3600 / 3600)

        fcst_solar = json.loads(self.batcontrol_fcst_solar)
        fcst_net_consumption = json.loads(self.batcontrol_fcst_net_consumption)
        net_consumption = convert_to_hourly_values(fcst_net_consumption, ts)
        production = convert_to_hourly_values(fcst_solar, ts)

        logging.debug("Net Consumption: %s", net_consumption)
        logging.debug("Production: %s", production)
        logging.debug("Ts Factor: %s", ts_factor)

        net_consumption[0] = net_consumption[0] * ts_factor
        production[0] = production[0] * ts_factor

        logging.debug("Net Consumption: %s", net_consumption)
        logging.debug("Production: %s", production)

        production_start_time = 0
        production_end_time = None
        for i, entry in enumerate(production):
            if production[i] > 0:
                production_start_time = i
                break

        for i in range(production_start_time, len(production)):
            if production[i] == 0:
                # Produktion ist in der vorherigen Stunde vorbei
                production_end_time = i - 1
                break
        if production_end_time is None:
            production_end_time = len(production)

        production_end_time = min(production_end_time, len(net_consumption))
        logging.debug("Production Start Time: %s", production_start_time)
        logging.debug("Production End Time: %s", production_end_time)

        sum_net_consumption = 0
        # Simple Summe.
        # Alternativ könnte man den Batterieverbrauch auch mit einbeziehen wenn
        # die sum_net_consumption < 0 ist.
        # Da batcontrol aber die Batterie eventuell sperrt, ist das "vorberechnen"
        # wenig sinnvoll.
        #
        for i in range(production_start_time, production_end_time):
            sum_net_consumption += net_consumption[i]
        sum_net_consumption = sum_net_consumption * -1
        if sum_net_consumption < 0:
            sum_net_consumption = 0

        logging.debug("Sum Net Production: %s", sum_net_consumption)

        free_capacity = (self.batcontrol_max_capacity *
                         self.em_config.capacity_utilization) - self.batcontrol_stored_energy
        difference = free_capacity -  sum_net_consumption
        logging.info("Freie Speicherkapazität (%.0f%%) %.2f , netto Produktion %.2f , = %.2f",
                     self.em_config.capacity_utilization*100, free_capacity, sum_net_consumption, difference)

        if difference < 0:
            if difference > -120:
                self.__set_feedin_max(120, "Difference = -120 - 0")
            elif difference > -1000:
                self.__set_feedin_max(1000, "Difference = -1000 - -120")
            else:
                self.__set_feedin_max(self.em_config.power_feed_in_max * -1 , "Difference < -1000")

            return True
        return False

    def is_enough_stored(self):
        return ( self.soc > (self.em_config.capacity_utilization * 100 ))

    def __is_em_mode_valid(self):
        return self.current_em_mode in ("0", "1")

    def __disable_em(self):
        self.update_em_mode(0)
        self.update_em_power(0)

    def __em_is_active(self):
        return self.current_em_mode == "1"

    def __em_is_inactive(self):
        return self.current_em_mode == "0"

    def __is_ev_likes_to_charge(self):
        # off, now, minpv, pv
        return ( self.ev_connected == "true" and self.ev_charge_mode in ("now", "minpv", "pv"))

    def __is_discharge_blocked_by_batcontrol(self):
        return self.batcontrol_mode == "0"

    def __set_feedin_max(self, power, reason):
        if power > 0:
            logging.error("Power Feed-In Max muss negativ sein %.2f" , power)
            return
        logging.debug("Set Power Feed-In Max to %.2f (%s)", power, reason)
        self.power_feed_in_max = power

    def evaluate(self):
        if self.__is_em_mode_valid() is False:
            return

        if self.received_fcst is False:
            logging.info("No forecast data received yet")
            return

        if self.batcontrol_status == "offline":
            logging.info("Batcontrol offline")
            return

        if self.sleep_interval > 0:
            self.sleep_interval -= 1
            return

        if not self.is_enough_stored():
            if not self.is_solar_ueberschuss_expected():
                logging.info(
                    "Kein Solarüberschuss erwartet, Deaktiviere Steuerung")
                self.sleep_interval = self.em_config.sleep_interval_no_solar
                if self.__em_is_active():
                    self.__disable_em()
                return
        else:
            soc_diff = self.soc - (self.em_config.capacity_utilization * 100)
            available_capacity = self.batcontrol_max_capacity * (soc_diff / 100)
             # Vermeide starkes Überschiessen rund um den Grenzwert.
            self.__set_feedin_max(available_capacity * - 1.5, "Capacity * -1.5")

        if self.z1_refreshed is False:
            logging.error("z1_zaehler is not set. Skip evaluation")
            return

        if self.__is_discharge_blocked_by_batcontrol():
            logging.info("Batcontrol Mode 0, disable WP EM Adjustment")
            if self.__em_is_active():
                self.__disable_em()
            return

        if self.__em_is_active():
            if self.__is_ev_likes_to_charge():
                logging.info("EV just Connected, disable EM")
                self.__disable_em()
                return

            if self.grid_power > self.em_config.grid_power_threshold:
                self.update_em_power(0)
                return

            if self.soc <= self.em_config.soc_threshold:
                self.__disable_em()
                return

            delta_power = self.grid_power - self.z1_zaehler
            logging.info("Delta Power: %.2f", delta_power)

            if delta_power > self.em_config.delta_power_difference_max:
                self.update_em_power(0)
                return

            if self.pv_power > self.em_config.pv_power_threshold:
                if self.soc < self.em_config.high_soc_threshold:
                    delta_power = max(delta_power, -1 *
                                      (self.pv_power - self.home_power))
            else:
                logging.info("PV Power %s unter Schwellenwert %s",
                             self.pv_power, self.em_config.pv_power_threshold)
                self.__disable_em()
                return

            # Delta_power is always negative for feed-in to wp
            delta_power = max(delta_power, self.em_config.power_feed_in_max * -1 , self.power_feed_in_max)

            # z1_zaehler nur einmal verwenden
            # Update ist unverlässlich
            self.z1_refreshed = False
            self.update_em_power(delta_power)

        if self.__em_is_inactive():
            if self.__is_ev_likes_to_charge():
                logging.info("EV Connected, do nothing")
                self.sleep_interval = self.em_config.sleep_interval_car
                return

            if self.soc > self.em_config.soc_threshold:
                self.update_em_mode(1)
            else:
                logging.info("Warten bis SOC > %s aktuell: %s",
                             self.em_config.soc_threshold, self.soc)

            if self.pv_power > self.em_config.pv_power_threshold:
                self.update_em_mode(1)

if __name__ == '__main__':
    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()

    # Set up logging based on debug flag
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    # Lese die Konfiguration aus config.yaml ein
    with open("config.yaml", "r") as config_file:
        config = yaml.safe_load(config_file)
    try:
        wp_em_adjustment = WP_EM_Adjustment(config)
    except Exception as e:
        logging.error(f"Failed to initialize WP_EM_Adjustment: {e}")
        exit(1)

    try:
        while True:
            time.sleep(5)
    finally:
        wp_em_adjustment.stop()
        logging.info("WP_EM_Adjustment stopped")