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
    # EV Sommer-Modus
    # Wenn dieser schalter an ist, wird die Regelung bei pv und minpv nicht deaktiviert.
    # Sobald mehr als 300W WP Versorgung anliegen, wird die Leistung der Wallbox reduzert..
    ev_summer_mode: bool = True
    # Maximale Leistung der Wallbox
    #    Wenn die Wallbox mehr als 300W benötigt, wird die WP Leistung reduziert.
    ev_max_power: int = 16    # Ampere
    ev_reduced_power: int = 6 # Ampere
    # How much we want to account usable_capacity
    usable_capacity_usage: float = 0.85

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
            "batcontrol_stored_usable_energy": float,
            # Diese Werte sind JSON-Strings, die später in Funktionen konvertiert werden
            "batcontrol_fcst_solar": lambda x: x,
            "batcontrol_fcst_net_consumption": lambda x: x,
        }
        self.send_topics = None
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
        self.batcontrol_stored_usable_energy = 0
        self.batcontrol_fcst_solar = "[]"
        self.batcontrol_fcst_net_consumption = "[]"
        self.received_fcst = False
        self.z1_refreshed = False

        # Batterie Verbrauchs-Wunsch
        #    False = nur PV Überschuss
        #    True = Akku und PV
        self.use_battery = False
        # Welche verbrauchsphase haben wir? Morgen, PV , Abend
        #    Morgen = 0
        #    PV = 1
        #    Abend = 2
        self.consumption_phase = 0



        # Lade die EnergyManagement-Konstanten aus der Config (falls vorhanden)
        em_consts = self.config.get('em_constants', {})
        self.em_config = EnergyManagementConstants(**em_consts)

        self.__set_feedin_max( self.em_config.power_feed_in_max , "Initial")

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

        # optional
        self.send_topics = self.config['mqtt'].get('send_topics', {})

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

        if self.send_topics.get('em_constant_base_topic', None) is not None:
            # Publiziere jeden Eintrag von EnergyManagementConstants aus
            # self.em _config als einzelnes topic + retained
            for key, value in self.em_config.__dict__.items():
                topic = "%s/%s" % (self.send_topics['em_constant_base_topic'], key)
                if self.dry_run:
                    logging.info("Dry run: on_connect would publish %s to %s", value, topic)
                else:
                    self.client.publish(topic, value, retain=True)
                logging.info("Published %s to %s", value, topic)

    def on_message(self, client, userdata, msg):
        if msg.payload == b"":
            logging.warning(f"Received empty payload for topic {msg.topic}")
            return
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
                elif key == "ev_connected":
                    if self.ev_connected == "true":
                        if converted == "false":
                            logging.info("EV disconnected")
                            self.sleep_interval = 0
                elif key == "ev_charge_mode":
                    if self.ev_charge_mode != converted:
                        logging.info("EV charge mode changed")
                        if converted != "off":
                            self.evaluate()
                        else:
                            self.sleep_interval = 0
                            self.evaluate()
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

    def enable_zp(self):
        """ Starte die Zirukulationspumpe um Warmwasser im Haus zu verteilen. """
        if self.send_topics.get('activate_zp', None) is None:
            return

        if self.dry_run:
            logging.info(
                f"Dry run: enable_zp would publish 1 to {self.send_topics.get('activate_zp')}")
        else:
            self.client.publish(self.send_topics['activate_zp'], 1)

    def update_em_mode(self, mode):
        if self.current_em_mode == mode:
            return
        if self.dry_run:
            logging.info(
                f"Dry run: update_em_mode would publish {mode} to {self.topics.get('set_em_mode')}")
        else:
            self.client.publish(self.topics['set_em_mode'], mode)

    def set_feed_in(self, power):
        """ Set the amount of feed in"""
        if power < 0:
            logging.error("Power Feed-In muss positiv sein %.2f", power)
            return

        # always send_to current_em_power on mqtt for statistics
        if self.send_topics.get('current_power', None) is not None:
            if self.dry_run:
                logging.info(
                    f"Dry run: update_em_power would publish {power} to {self.send_topics.get('current_em_power')}")
            else:
                self.client.publish(self.send_topics['current_power'], power)
            logging.debug("Published %s to %s", power, self.send_topics['current_power'])

        max_power = min ( power, self.power_feed_in_max)
        self.update_em_power(max_power * -1)
        # Publish to mqtt power_feed_in
        #   send_topics is initialied later
        if self.send_topics.get('power_feed_in', None) is not None:
            if self.dry_run:
                logging.info(
                    f"Dry run: set_feed_in would publish {max_power} to {self.send_topics.get('power_feed_in')}")
            else:
                self.client.publish(self.send_topics['power_feed_in'], max_power)
            logging.debug("Published %s to %s", max_power, self.send_topics['power_feed_in'])

    def update_em_power(self, power):
        """
        Set the energy management power to the specified value.

        Args:
            power (int): The power value to set.
        """
        power = int(round(power, 0))
        current_power = int(round(self.current_em_power, 0))
        # Wir reizen das maximum komplett aus, ansonsten reichen Näherungswerte
        new_power=int(round(power, 0))
        if power != self.power_feed_in_max:
            if abs(power - current_power) <= abs(current_power * self.em_config.power_tolerance_percent):
                logging.debug("Power is already set to ~ %s ; %.3f",
                            power, float(self.current_em_power))
                return
            new_power = int(round(( power + current_power ) / 2, 0))

        logging.info("Set EM Power to %s", new_power)
        if self.dry_run:
            logging.info(
                f"Dry run: update_em_power would publish {new_power} to {self.topics.get('set_em_power')}")
        else:
            self.client.publish(self.topics['set_em_power'], new_power)
        self.last_delta_power = new_power

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

        #for i in range(production_start_time, production_end_time):
        #    sum_net_consumption += net_consumption[i]
        #  sum_net_consumption = sum_net_consumption * -1
        #if sum_net_consumption < 0:
        #    sum_net_consumption = 0

        # Es gibt drei Szenarien die hier relvant sind:
        #  1. Wir produzieren *jetzt* gerade mehr als wir bis Ende Produktion
        #     in den Akku laden und verbrauchen können.
        #  2. Wir werden gleich anfangen zu produzieren und haben mehr als wir
        #     bis dahin verbrauchen können.
        #  3. Wir sind am Ende der Produktion und haben die Nacht vor uns.

        if production_start_time == 0:
            # Wir produzieren jetzt
            sum_net_consumption = np.sum(net_consumption[production_start_time:production_end_time])
            self.consumption_phase = 1
        elif production_start_time <= 4:
            # Wir produzieren gleich
            sum_net_consumption = np.sum(net_consumption[0:production_end_time])
            self.consumption_phase = 0
        else:
            # Wir sind am Ende der Produktion, hier summieren wir den Verbrauch
            # bis zur Produktion.
            sum_net_consumption = np.sum(net_consumption[0:production_start_time])
            self.consumption_phase = 2

        logging.info("Consumption Phase: %s", self.consumption_phase)
        # Positiv, wenn wir mehr verbrauchen als produzieren.
        # Negativ, wenn wir mehr produzieren als verbrauchen.
        logging.debug("Sum Net Consumption: %s", sum_net_consumption)
        if sum_net_consumption < 0:
            sum_net_production = sum_net_consumption * -1
        else:
            sum_net_production = 0

        free_capacity = (self.batcontrol_max_capacity *
                         self.em_config.capacity_utilization) - self.batcontrol_stored_energy

        if free_capacity < 0:
            free_capacity = 0
            self.use_battery = True

        difference = 0
        if self.consumption_phase != 2:
            difference = free_capacity -  sum_net_production
            logging.info("Produktion: Freie Speicherkapazität (%.0f%%) %.2f , netto Produktion %.2f , = %.2f",
                        self.em_config.capacity_utilization*100, free_capacity, sum_net_production, difference)
        else:
            use_energy = self.batcontrol_stored_usable_energy * self.em_config.usable_capacity_usage
            difference = sum_net_consumption -  use_energy
            logging.info("Verbrauch: Gespeichert  %.2f , Verbrauch %.2f , = %.2f",
                      use_energy , sum_net_consumption, difference)

        # send to mqtt solar_surplus
        if self.send_topics.get('solar_surplus', None) is not None:
            surplus = 0 if difference > 0 else difference * -1
            if self.dry_run:
                logging.info(
                    f"Dry run: is_solar_ueberschuss_expected would publish {surplus} to {self.send_topics.get('solar_surplus')}")
            else:
                self.client.publish(self.send_topics['solar_surplus'], surplus)
            logging.debug("Published %s to %s", surplus, self.send_topics['solar_surplus'])

        if difference < 0:
            if difference > -500:
                self.use_battery = False
                self.__set_feedin_max(120, "Difference = -120 - 0")
            elif difference > -1500:
                self.use_battery = False
                self.__set_feedin_max(1000, "Difference = -1000 - -120")
            else:
                self.use_battery = True
                self.__set_feedin_max(self.em_config.power_feed_in_max , "Difference < -1000")

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
        ready_to_charge = ( self.ev_connected == "true" and self.ev_charge_mode in ("now", "minpv", "pv"))
        # send to car_ready_to_charge
        if self.send_topics.get('car_ready_to_charge', None) is not None:
            if self.dry_run:
                logging.info(
                    f"Dry run: __is_ev_likes_to_charge would publish {ready_to_charge} to {self.send_topics.get('car_ready_to_charge')}")
            else:
                self.client.publish(self.send_topics['car_ready_to_charge'], ready_to_charge)
            logging.debug("Published %s to %s", ready_to_charge, self.send_topics['car_ready_to_charge'])
        return ready_to_charge

    def __is_discharge_blocked_by_batcontrol(self):
        return self.batcontrol_mode == "0"

    def __set_feedin_max(self, power, reason):
        if power < 0:
            logging.error("Power Feed-In Max muss positiv sein %.2f" , power)
            return
        logging.debug("Set Power Feed-In Max to %.2f (%s)", power, reason)
        self.power_feed_in_max = power
        # publish to mqtt
        if self.send_topics is not None and \
           self.send_topics.get('em_constant_base_topic', None) is not None:
            topic = "%s/power_feed_in_max" % self.send_topics['em_constant_base_topic']
            if self.dry_run:
                logging.info("Dry run: __set_feedin_max would publish %s to %s", power, topic)
            else:
                self.client.publish(topic, power, retain=True)
            logging.debug("Published %s to %s", power, topic)

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
            self.use_battery = True
            self.__set_feedin_max(self.em_config.power_feed_in_max, "Maximum Battery is used")
#        else:
#            soc_diff = self.soc - (self.em_config.capacity_utilization * 100)
#            available_capacity = self.batcontrol_max_capacity * (soc_diff / 100)
#             # Vermeide starkes Überschiessen rund um den Grenzwert.
#            self.__set_feedin_max(available_capacity * 1.5, "Capacity * 1.5")

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

            # Wir wollen die WP-Leistung als positiven Wert benutzen.
            wp_power =  int(round(self.z1_zaehler - self.grid_power,0))
            logging.info("WP Power: %.2f", wp_power)

            # Wenn wir doch negetavie Leistung beziehen, dann beziehen wir
            # im Hause gerade schon mehr als 100 W und wollen nix machen.
            if wp_power < -1 * self.em_config.delta_power_difference_max:
                logging.info("Netzbezug zu hoch (%.2fW) zu hoch, deaktiviere Regelung", wp_power)
                self.update_em_power(0)
                return

            if not self.use_battery:
                if self.pv_power > self.em_config.pv_power_threshold:
                    if self.soc < self.em_config.high_soc_threshold:
                        available_pv_power = self.pv_power - self.home_power
                        if available_pv_power < 0 and not self.use_battery:
                            # Home bedarf mehr als PV liefert
                            self.update_em_power(0)
                            logging.info("kein Solarüberschuss, SOC < %d, PV < Home",
                                            self.em_config.high_soc_threshold)
                            return
                        logging.debug("Available PV Power: %.2f", available_pv_power)
                        wp_power = min(wp_power, available_pv_power)
                else:
                    logging.info("PV Power %s unter Schwellenwert %s; keine Batterie",
                                self.pv_power, self.em_config.pv_power_threshold)
                    self.__disable_em()
                    return
            else:
                logging.debug("Batterie wird genutzt, WP Power: %.2f", wp_power)

            # Delta_power)
            logging.debug("%s , %s , %s " , wp_power , self.em_config.power_feed_in_max , self.power_feed_in_max)
            wp_power = min(wp_power, self.em_config.power_feed_in_max, self.power_feed_in_max)

            # z1_zaehler nur einmal verwenden
            # Update ist unverlässlich
            self.z1_refreshed = False
            self.set_feed_in(wp_power)
            self.enable_zp()

        if self.__em_is_inactive():
            if self.__is_ev_likes_to_charge():
                logging.info("EV Connected, do nothing")
                self.sleep_interval = self.em_config.sleep_interval_car
                return

            if not self.soc > self.em_config.soc_threshold:
                logging.info("Warten bis SOC > %s aktuell: %s",
                            self.em_config.soc_threshold, self.soc)
                return

            if not self.use_battery:
                if not self.pv_power > self.em_config.pv_power_threshold:
                    logging.info("Warten bis PV Power > %s aktuell: %s",
                                self.em_config.pv_power_threshold, self.pv_power)
                    return

                available_pv_power = self.pv_power - self.home_power
                if available_pv_power < 0:
                    logging.debug("Kein Solarüberschuss, PV < Home")
                    return

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