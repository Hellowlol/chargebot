import math
from datetime import datetime, time, timedelta
from functools import partial, partialmethod
from operator import itemgetter

import hassapi as hass


"""
Chargebot

This is charge bot tailored for my use, this used a combination of easee, tesla and nordpool integration
in home assistant. It should be easy to change this to your usage.

Example config here...
charge_bot:
  module: chargebot
  class: Chargebot

  # On and off button in ha.
  load_balance: input_boolean.car_load_balance
  smart_charging: input_boolean.car_smart_charging

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
  # should be pause or stop
  charger_service_start: "easee/start"
  charger_service_end: "easee/pause"
  charger_status_entity: "sensor.easee_charger_eh385021_status"
  charger_status_old: "CONNECTED"
  charger_status_new: "READY_TO_CHARGE"
  charger_status_charging: "CHARGING"
  charger_max_speed_kwh: 11.0 # kwh

  ### Car options ###
  # optional, default to 0 the state cant be reached
  car_battery_sensor_entity: "sensor.tesla_model_3_battery_sensor"

  car_battery_size_kwh: 75.0 # kwh
  verify_car_connected_and_home: true # Set this to false
  # optional
  car_device_tracker_entity: device_tracker.tesla_model_3_location_tracker
  car_connected_to_charger: "binary_sensor.tesla_model_3_charger_sensor"

"""


def get_continues_timespan(data):
    data = sorted(data, key=itemgetter("start"))
    m = None
    xam = None
    result = []
    for i, d in enumerate(data):
        if m is None:
            m = d["start"]
        if xam is None:
            xam = d["end"]

        try:
            if d["start"] == data[i + 1]["start"] - timedelta(hours=1):
                xam = data[i + 1]["end"]
            else:
                result.append((m, xam))
                m = None
                xam = None
        except IndexError:
            if m is not None:
                result.append((m, d["end"]))

    return result


class Chargebot(hass.Hass):
    def initialize(self):
        # DUMP args.
        for arg in self.args:
            self.log("%s %s", arg, self.args[arg])
        self._time_over_limit = 0
        self._charger_id = "EH385021"  # Is this really needed?
        self.charge_plan = []
        self.app_callbacks = []
        self._has_been_limited = None

        self.handle_cb_load_balance = self.listen_state(
            self.load_balance_cb, self.args["power_usage_in_w"]
        )
        self.handle_cb_charge_plan = self.listen_state(
            self.chargeplan_cb,
            self.args["charger_status_entity"],
            old=self.args["charger_status_old"],
            new=self.args["charger_status_new"],
        )

        # Add some callbacks
        self.app_callbacks.append(self.handle_cb_charge_plan)
        self.app_callbacks.append(self.handle_cb_load_balance)
        # To be able to cancel callbacks.
        self.chargeplan_handles = []
        self._has_been_limited = None

        # Only for man testing atm, should run when the using the cb or schedule
        # after the prices for tomorrow is available.
        # self.create_a_charge_plan()
        # self.chargeplan_cb("kek", "all", None, None, {})

        # Lets add this as a service so it can get
        # executed manually using the api
        self.register_service(
            "chargebot/create_a_charge_plan", self.create_a_charge_plan
        )

    def verify_car(self):
        """verify that the car is home and connected to a charger."""
        if self.get_tracker_state(self.args["car_device_tracker_entity"]) != "home":
            self.log("Didn't execute as your car isnt home.")
            return False

        if self.get_state(self.args["car_connected_to_charger"]) != "on":
            self.log("Didnt execute as your car isnt connected")
            return False

        return True

    def clean_up(self):
        """Cancel all listeners and cancel everything."""
        # untested
        self.cancel_listen_state(self.handle_cb_load_balance)
        self.cancel_listen_state(self.handle_cb_charge_plan)
        self.cancel_change_plans()

    def cancel_change_plans(self):
        # untested
        if len(self.chargeplan_handles):
            for handle in self.chargeplan_handles:
                self.cancel_listen_state(handle)

    def chargeplan_cb(self, entity, attribute, old, new, kwargs):
        if self.create_a_charge_plan() is True:
            if len(self.chargeplan):
                for start, end in self.chargeplan:
                    # Debugging
                    # now = self.datetime(aware=True)
                    # start = now + timedelta(seconds=30)
                    # end = now + timedelta(seconds=60)

                    def execute_service(data={}):
                        # return self.call_service("notify/notify", title = "Hello", message=data["service_type"])
                        if self.args["verify_car_connected_and_home"] is True:
                            if self.verify_car() is True:
                                return self.call_service(data["service_type"])
                        else:
                            return self.call_service(data["service_type"])

                    self.log("Added starting charging at %s and stop at %s", start, end)
                    start_handle = self.run_at(
                        execute_service,
                        start,
                        service_type=f"{self.args['charger_service_start']}",
                    )
                    end_handle = self.run_at(
                        execute_service,
                        end,
                        service_type=f"{self.args['charger_service_end']}",
                    )
                    self.chargeplan_handles.append(start_handle)
                    self.chargeplan_handles.append(end_handle)

        else:
            self.log("No chargeplan")

    def create_a_charge_plan(self):
        """Create a chargeplan"""

        if self.get_state(self.args["smart_charging"], default="off") == "off":
            self.log("smart_charing is off")
            return False

        now = self.datetime(aware=True)
        ip_state = self.get_state(self.args["charger_ready_at"])
        ready_until = self.parse_datetime(ip_state, aware=True)

        if ready_until is not None and now > ready_until:
            ready_until = ready_until + timedelta(days=1)

        # get soc of the car
        car_soc = float(
            self.get_state(self.args["car_battery_sensor_entity"], default=0)
        )
        car_kwh_battery = float(self.args["car_battery_size_kwh"])  # Config option
        # Based on the onboard charger in the car.
        max_charge_speed = float(self.args.get("charger_max_speed_kwh", 11.0))

        state = self.get_state(self.args["power_price_entity"], attribute="all")
        tomorrow = state.get("attributes", {}).get("raw_tomorrow", [])
        today = state.get("attributes", {}).get("raw_today", [])
        currency = state.get("attributes", {}).get("currency")
        possible_hours = today + tomorrow
        str_format = "%Y-%m-%dT%H:%M:%S%z"

        avail_hours = []
        for i in possible_hours:
            start = datetime.strptime(i["start"], str_format)
            end = datetime.strptime(i["end"], str_format)
            # Lets skip all hours that is already passed.
            if now > start:
                # self.log("skipped %s bc hour is passed", start)
                continue
            elif ready_until is not None and start > ready_until:
                # elf.log("skipped %s cb hour is after the car should be ready", start)
                continue
            else:
                data = {"start": start, "end": end, "value": i["value"]}
                if i["value"] is not None:
                    # self.log("data %s", data)
                    avail_hours.append(data)

        if len(avail_hours):
            number_of_kwh_to_charge = car_kwh_battery / 100 * car_soc
            numbers_of_hours_required_to_be_fully_charged = math.ceil(
                number_of_kwh_to_charge / max_charge_speed
            )

            cheapest_hours = sorted(avail_hours, key=itemgetter("value"))[
                :numbers_of_hours_required_to_be_fully_charged
            ]

            # This need to be more presice.
            # we dont include nettleie 35,79 kwh
            # the monthly fee is 315 ish
            # also we can get amount of juice added using the charger or the car.
            cost = 0
            for ch in cheapest_hours:
                cost += float(ch["value"])

            # Create a chargeplan with continues start and end as cars/chargers
            # dont like to get stopped/started
            chargeplan = get_continues_timespan(cheapest_hours)
            # For tesla is possible to get a sensor with time remaining until
            # soc in reached the charge limit.

            for part_plan in chargeplan:
                self.log(
                    "Charge should start at %s and end at %s",
                    part_plan[0],
                    part_plan[1],
                )
            self.log("Total cost should be %s %s", cost, currency)

            self.chargeplan = chargeplan
            if len(chargeplan):
                return True
            else:
                return False

    def load_balance_cb(self, entity, attribute, old, new, kwargs):
        """Callback used to manage the loadbalance"""
        if self.get_state(self.args["load_balance"], default="off") == "off":
            return

        pw_state = self.get_state(self.args["power_usage_in_w"])

        if float(pw_state) >= self.args["main_fuse"] * self.args["volt"] * math.sqrt(
            self.args["phase"]
        ):
            charger_state = self.get_state(self.args["charger_status_entity"])
            # https://github.com/fondberg/easee_hass/
            if charger_state == self.args["charger_status_charging"]:
                self.call_service(
                    self.args["charger_service_end"], charger_id=self._charger_id
                )
                self._has_been_limited = True
                self.log("should have paused")
            else:
                self.log("Over limit but the charger isnt charging..")

        else:
            # Should add some kind of cool down here..
            # self.log("TICK %s" % pw_state)
            if self._has_been_limited is True:
                self.log("started charger again")
                # Start the shit back up.
                self._has_been_limited = False
                self.call_service(
                    self.args["charger_service_end"], charger_id=self._charger_id
                )
