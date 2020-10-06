# chargebot
chargebot for appdaemon

This is chargebot that adds smart charging to any charger that can be controlled using home assistant. (only tested on easee)

# Features
- Create a chargeplan
- A changeplan is a timeperiod of the cheapest hours to fill your car with energy to the wanted soc before a certain time in the future
- Loadbalance the energy usage so you don't blow your main fuse.

Integrations in HA that is required.
- nordpool (to get the energy prices)
- a sensor that you can get watt usage in real time
- others are needed to, see the config example for a full list.


## Example config
```
charge_bot:
  module: chargebot
  class: Chargebot

  # On and off button in ha.
  load_balance: input_boolean.car_load_balance
  smart_charging: input_boolean.car_smart_charging

  # Send a notification to something
  # This can be false (all notifications disabled), true (default service)
  # or a str like persistent_notification or smtp etc.
  notify: true


  ### POWER STUFF ###
  power_price_entity: "sensor.nordpool_kwh_krsand_nok_3_10_025"
  power_usage_in_w: "sensor.mqtt_relay_energy_usage"
  # float in atp ex 63.0
  main_fuse: 63.0
  # float
  volt: 230.0
  # float
  phase: 3.0

  ### Charger options ###
  charger_ready_at: "input_datetime.car_ready_at"
  charger_service_start: {"service": "easee/start", "data": {charger_id: "EH385021"}}
  charger_service_end: {"service": "easee/stop", "data": {charger_id: "EH385021"}}
  charger_status_entity: "sensor.easee_charger_eh385021_status"
  charger_status_old: "CONNECTED"
  charger_status_new: "READY_TO_CHARGE"
  charger_status_charging: "CHARGING"
  charger_max_speed_kwh: 11.0 # kwh this

  ### Car options ###
  # optional, default to 0 the state cant be reached
  car_battery_sensor_entity: "sensor.tesla_model_3_battery_sensor"
  car_battery_size_kwh: 72.5 # kwh
  verify_car_connected_and_home: true # Set this to false
  # optional
  car_device_tracker_entity: device_tracker.tesla_model_3_location_tracker
  car_connected_to_charger: "binary_sensor.tesla_model_3_charger_sensor"
```
