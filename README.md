# batcontrol_wp-feedin

Dieses Projekt steuert die Eigenverbrauchsregelung einer Photovoltaik-Anlage mit Solarspeicher über die batcontrol MQTT-API.

## Überblick

Das System basiert auf einer PV-Anlage mit integriertem Batteriespeicher, die den Solarstrom optimal im Haushalt zur Verfügung stellt. Es werden folgende Komponenten verwendet:

- **PV-Anlage & Batteriespeicher:** Liefert Solarstrom und speichert überschüssige Energie.
- **Smart Meter:** Misst den Stromfluss vom Haus.
- **Zaehler 2 & Zaehler 1:** Erfassung des Stromverbrauchs innerhalb einer Zählerkaskade.
- **Wärmepumpe (WP):** Wird über einen Zwischenzaehler versorgt – hier fließt PV-Strom gezielt in deren Versorgung, da der WP einen günstigeren Stromtarif bezieht.

## Strompfad

Der Strompfad im System sieht wie folgt aus:

```
PV + Battery -> Home -> SmartMeter -> Zaehler 2 -> WP -> Zaehler 1
```


Dies stellt eine typische Zählerkaskade dar.

## Datenerfassung und MQTT-Topics

Die Steuerung basiert auf verschiedenen Datenströmen, die alle 30 Sekunden aktualisiert werden:

- **evcc liefert:**
  - `soc`: Batteriekapazität (State of Charge)
  - `grid_power`: Strombezug oder Einspeisung an Zaehler 2/Smartmeter
  - `ev_connected`: Gibt an, ob ein Elektroauto angeschlossen ist
  - `pv_power`: Solarstromproduktion
  - `home_power`: Verbrauch im Haushalt

- **Daten aus batcontrol:**
  - Solarertrag
  - Verbrauch minus Solarertrag

Diese Daten werden über die batcontrol MQTT-API bezogen. Insbesondere werden die Werte über das Topic `z1_zaehler` gelesen, um den aktuellen Verbrauch auf Zaehler 2 zu ermitteln.

## Regelungslogik

Die Regelung erfolgt basierend auf folgenden Parametern:

- **Tagesproduktion:** Es wird errechnet, wie viel Strom am Tag produziert wird.
- **Startkriterium:** Sobald der prognostizierte Tagesertrag den konfigurierten Wert (`batcontrol_max_capacity`) überschreitet und ein SoC von mindestens 50 % erreicht ist, beginnt die Regelung.
- **Netzbezugsermittlung:** Aus `grid_power` (genannt z2) wird berechnet, was zwangsweise eingespeist wird, um die Wärmepumpe mit PV-Strom zu versorgen.
- **Regelungsmodus:**
  - Bis zu einem SoC von 90 % wird ausschließlich überschüssiger PV-Strom verwendet.
  - Ab einem SoC von 90 % wird zusätzlich die Batterie herangezogen.

Die Regelung nutzt die Funktion des Eigenverbrauchsmanagements (EM) von Fronius sowie einen verschobenen Regelpunkt.

## Konfiguration

Die MQTT-Konfiguration wird in der Datei [config/batcontrol_config.yaml](config/batcontrol_config.yaml) bzw. der Dummy-Konfiguration in [config/batcontrol_config_dummy.yaml](config/batcontrol_config_dummy.yaml) festgelegt. Dort sind u.a. die MQTT-Serverdaten und die entsprechenden Topics hinterlegt.

