import math
import re
import statistics
from datetime import datetime, timedelta
from operator import itemgetter

import hassapi as hass
from adbase import app_lock


"""
EaseeChargebot

This is charge bot tailored for my use, this used a combination of easee, tesla and nordpool integration
in home assistant. It should be easy to change this to your usage.

Example config

charge_bot:
  module: chargebot
  class: EaseeChargebot

  # input_boolean button in ha.
  # Optional
  load_balance: input_boolean.car_load_balance
  # Required
  smart_charging: input_boolean.car_smart_charging
  # Optional, if this key exists in the config the charger will be autolocked when a charge is done.
  charger_temp_override: input_boolean.charger_temp_override

  # Send a notification to something
  # This can be false (all notifications disabled), true (default service)
  # or a str like persistent_notification or smtp etc.
  notify: true


  ### POWER STUFF ###
  # All settings where is not required if load_balance is not used.
  # Optional, will be found by charge bot, you only need to fill it out if you have
  # more then one nordpool sensor.
  power_price_entity: "sensor.nordpool_kwh_krsand_nok_3_10_025"
  # Required
  power_usage_in_w: "sensor.mqtt_relay_energy_usage"
  # Required float in atp ex 63.0
  main_fuse: 63.0
  # Required float <= 1.0
  main_fuse_limit: 0.9
  # float, required
  volt: 230.0, reqiored
  # float, required
  phase: 3.0
  ### END POWERSTUFF ###

  ### Charger options ###
  charger_ready_at: "input_datetime.car_ready_at"
  charger_status_entity: "sensor.easee_charger_eh385021_status"
  # Optional
  charger_no_current_entity: "sensor.easee_charger_eh385021_reason_for_no_current"
  ## END charger options ###

  ### Car options ###
  # optional, default to 0 the state cant be reached
  car_battery_sensor: "sensor.tesla_model_3_battery_sensor"
  car_battery_size_kwh: 72.5 # kwh
  car_onboard_charger_kwh = 11.0
  verify_car_connected_and_home: true # Set this to false
  # Optional, required if verify_car_connected_and_home is true
  car_device_tracker_entity: device_tracker.tesla_model_3_location_tracker
  car_connected_to_charger: "binary_sensor.tesla_model_3_charger_sensor"

needed_ = ["car_onboard_charger_kwh", "car_onboard_charger_kwh"]
needed_inputs = ["charger_temp_override", "load_balance", "load_balance"]
req_entities_user_added = ["charger_ready_at", "car_battery_sensor", ""]
#entities = ["car_connected_to_charger", "charger_no_current_entity", "car_device_tracker_entity", "car_battery_sensor_entity", "charger_temp_override", "charger_ready_at"]
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


class EaseeChargebot(hass.Hass):
    def initialize(self):
        self.setup_config()
        # Chargeplans
        self.charge_plan = []
        # listener callbacks.
        self.app_callbacks = []
        self._serial = None
        # cancel timer callbacks.
        self.chargeplan_handles = []
        self._loadbalancer_last_value = None
        self._charger_paused_by_loadbalance = None

        self.PID = None

        # To simulate..
        self.simulate()
        return

        self.handle_cb_load_balance = self.listen_state(
            self.load_balance_cb, self.args["power_usage_in_w"]
        )

        self.handle_cb_charge_plan = self.listen_state(
            self.cb_charger_status, self.args["charger_status_entity"]
        )
        self.handle_cb_edit_ready_at = self.listen_state(
            self.cb_charger_ready_at, self.args["charger_ready_at"]
        )

        self.handle_cb_smart_charge = self.listen_state(
            self.cb_smart_charging, self.args["smart_charging"]
        )

        self.handle_cb_temp_allow = self.listen_state(
            self.cb_temp_allow, self.args["charger_temp_override"]
        )

        # Add some callbacks
        self.app_callbacks.append(self.handle_cb_charge_plan)
        self.app_callbacks.append(self.handle_cb_load_balance)
        self.app_callbacks.append(self.handle_cb_edit_ready_at)
        self.app_callbacks.append(self.handle_cb_smart_charge)
        self.app_callbacks.append(self.handle_cb_temp_allow)

        # Lets add this as a service so it can get
        # executed manually using the api
        self.register_service(
            "chargebot/create_a_charge_plan", self.reschedule_charge_plan
        )
        self.register_service("chargebot/cancel_change_plans", self.cancel_change_plans)

    def setup_config(self):
        """Try to find some settings for the user."""
        # This is way to messy. FIXME

        all_states = self.get_state()
        conf = {}

        serial = None
        OK = False

        entity_start_name = ""

        # Inputs the user has to create in home assistant
        needed_inputs = ["charger_temp_override", "smart_charging", "load_balance"]
        non_discoverable_entities = ["power_usage_in_w"]
        # Should be able to pull all this from the api later.
        charger_settings = ["main_fuse", "phase", "volt"]

        if_missing_add_as_false = ["notify"]

        # try to find the correct name
        for entity in all_states:
            if entity.startswith("sensor.easee") and entity.endswith("status"):
                # Seems to be some changes to the entity names..
                entity_start_name = entity.split(".")[1].replace("_status", "")
                serial = self.get_state(
                    "sensor.%s_status" % entity_start_name, attribute="id"
                )
                self._serial = serial.lower()
                self.args["serial"] = serial
                self.args["entity_start_name"] = entity_start_name
                break

        for config_arg in self.args:
            if config_arg in needed_inputs:
                if not self.entity_exists(self.args[config_arg]):
                    self.log(
                        "%s does not exists, will not be able to %s",
                        self.args[config_arg],
                        config_arg,
                        level="INFO",
                    )
                else:
                    needed_inputs.remove(config_arg)

        # Add any sensor that we can find.
        for entity in all_states:
            if entity == "sensor.%s_reason_for_no_current" % entity_start_name:
                conf["charger_no_current_entity"] = entity
            elif entity == "sensor.%s_status" % entity_start_name:
                conf["charger_status_entity"] = entity

            elif entity.startswith("sensor.nordpool"):
                conf["power_price_entity"] = entity

        # Add the one we found as default.
        for key, value in conf.items():
            if key not in self.args:
                self.args[key] = value

        self.log("Found %s using entities", conf, level="DEBUG")

        return OK

    def access_level(self, locked=True):
        """Change access level on the charger, this will also start and stop the charge."""
        serial = self.args["serial"]
        level = 2 if locked else 1
        service_call = {
            "service": "easee/set_charger_access",
            "data": {"charger_id": serial, "access_level": level},
        }
        return self.charger_service(service_call, verify=False)

    def cmd(self, service_name):
        return {"service": service_name, "data": {"charger_id": self.args["serial"]}}

    def stop_charge(self, verify=True):
        return self.charger_service(self.cmd("easee/stop"), verify=verify)

    def start_charge(self, verify=True):
        return self.charger_service(self.cmd("easee/start"), verify=verify)

    def pause_charge(self, verify=True):
        return self.charger_service(self.cmd("easee/pause"), verify=verify)

    def resume_charge(self, verify=True):
        return self.charger_service(self.cmd("easee/resume"), verify=verify)

    def toggle_charge(self, verify=True):
        return self.charger_service(self.cmd("easee/toggle"), verify=verify)

    def set_circuit_current_limit(self, value, verify=False):
        if self._loadbalancer_last_value is None:
            self._loadbalancer_last_value = value

        circuit_id = self.get_state(
            self.args["charger_status_entity"], attribute="circuit_id"
        )
        value = math.floor(float(value))
        self._loadbalancer_last_value = value
        call = {
            "service": "easee/set_circuit_dynamic_current",
            "data": {
                "circuit_id": circuit_id,
                "currentP1": value,
                "currentP2": value,
                "currentP3": value,
            },
        }
        self.log("%s", call, level="DEBUG")

        # Just for debugging so i can graph it
        self.set_value("input_number.kw_set_by_loadbalance", self.amp_to_watt(value) / 1000)
        self.set_value("input_number.amps_set_by_loadbalance", value)
        if value < 6:
            self.set_value("input_number.charger_paused_by_loadbalance", 0)
        else:
            self.set_value("input_number.charger_paused_by_loadbalance", 30)
        return self.charger_service(call, verify=verify)

    def cb_temp_allow(self, entity, attribute, old, new, kwargs):
        """Open the charger, this will also start the charge"""
        if new == "on":
            self.access_level(locked=False)
            self.notify(
                "Temporary unlocking the charger, it will be locked after charging is done."
            )

    def cb_charger_status(self, entity, attribute, old, new, kwargs):
        if old == new:
            self.log("same state..", level="DEBUG")
            return

        # Use this to get better error message when my pr is included.
        # no_current = self.get_state(self.args["charger_no_current_entity"])

        # Handle when the cars is connected or ready to charge.
        if old == "STANDBY" and new in ("READY_TO_CHARGE", "CAR_CONNECTED"):
            if (
                self.entity_exits("charger_temp_override")
                and self.get_state("charger_temp_override") == "on"
            ):
                self.access_level(False)
                self.log(
                    "Manual override is used, unlocking charger and relocking after charge is done."
                )
            else:
                self.reschedule_charge_plan()

        # Handle charge finished.
        # Check if this is precice enough, maybe READY_TO_CHARGE is skipped if the charger is
        # if disconnected before the next poll from the api.
        elif old == "CHARGING" and new == "READY_TO_CHARGE":
            self.notify("Charging is finished")
            if self.entity_exits("charger_temp_override"):
                # We want to check if the charger is locked or unlocked.
                # incase somebody uses the easee app to unlock. use HASS FFS!
                is_locked = self.get_state(
                    self.args["charger_status_entity"],
                    attribute="config.authorizationRequired",
                )

                if (
                    is_locked is False
                    or self.get_state("charger_temp_override") == "on"
                ):
                    self.turn_off("charger_temp_override")
                    self.access_level(True)
                    self.log("Locked the the charger")

        # The car was disconnected from the charger. So lets make sure we cancel any chargeplan.
        elif new == "STANDBY":
            self.cancel_change_plans()

        elif old == "CHARGING" and new == "PAUSED":
            if self._charger_paused_by_loadbalance is True:
                self.log(
                    "The charger was paused by loadbalance, just logging it #fixme",
                    level="INFO",
                )
                # Should we reschedule the chargeplan ?

    def cb_smart_charging(self, entity, attribute, old, new, kwargs):
        """Handle create a chargeplan or cancel depending on the state change."""
        if old == "off" and new == "on":
            # this can "fail" if the car isnt connected.
            self.reschedule_charge_plan()
        elif old == "on" and new == "off":
            self.cancel_change_plans()

    def notify(self, message, **kwargs):
        """Send a a notification and log it."""
        notify = self.args["notify"]
        if notify is not False:
            if isinstance(notify, str):
                if "persistent_notification" in notify:
                    self.persistent_notification(message, **kwargs)
                else:
                    # Some fixups for retards like me
                    # that copy pasted from HA service tab.
                    notify = notify.replace("notify", "")
                    if notify.startwith((".", "/")):
                        notify = notify[1:]
                    super().notify(message, name=notify, **kwargs)
            else:
                super().notify(message, **kwargs)

        self.log(message, level="DEBUG")

    def charger_service(self, data, verify=True, notify=False):
        """Send a service call to the charger."""
        service = data.pop("service")
        kw = data.pop("data", {})
        call = None
        if self.args["verify_car_connected_and_home"] is True and verify is True:
            if self.verify_car() is True:
                call = self.call_service(service, **kw)
        else:
            call = self.call_service(service, **kw)

        if call is not None:
            if notify is True:
                self.notify("Sent charge service")
            return call

    def verify_car(self):
        """verify that the car is home and connected to a charger."""
        if self.get_tracker_state(self.args["car_device_tracker_entity"]) in (
            "home",
            "on",
        ):
            self.notify("Didn't execute as your car isnt home.")
            return False

        if self.get_state(self.args["car_connected_to_charger"]) != "on":
            self.notify("Didnt execute as your car isnt connected")
            return False

        return True

    def terminate(self):
        """Cancel all listeners and cancel everything."""
        return
        # untested
        for handle in self.app_callbacks:
            self.cancel_listen_state(handle)
        self.app_callbacks.clear()
        self.cancel_change_plans()

    def cb_charger_ready_at(self, entity, attribute, old, new, kwargs):
        """Callback that executes when charger_ready_at is changed in
        home assistant. This cancels a chargeplan that is queued up
        and creates a new one.
        """
        if old == new:
            self.log("same state..")
            return
        self.reschedule_charge_plan()

    def reschedule_charge_plan(self, *args, **kwargs):
        """Reschedule and recreate a chargeplan."""
        self.log("Reschedule changeplans", level="DEBUG")
        self.cancel_change_plans()
        self.create_and_schedule_chargeplan()

    def cancel_change_plans(self, *args, **kwargs):
        """Cancel all chargeplans and timers."""
        self.log("Canceling chargeplans", level="DEBUG")
        if len(self.chargeplan_handles):
            for handle in self.chargeplan_handles:
                self.cancel_timer(handle)
            self.chargeplan_handles.clear()

    def create_and_schedule_chargeplan(self):
        """Create and schedule chargeplans."""
        self.log("called create_and_schedule_chargeplan", level="DEBUG")
        if self.create_a_charge_plan() is True:
            if len(self.chargeplan):
                for start, end in self.chargeplan:
                    self.log(
                        "Added starting charging at %s and stop at %s",
                        start,
                        end,
                        level="DEBUG",
                    )

                    start_handle = self.run_at(self.start_charge, start)
                    end_handle = self.run_at(self.stop_charge, end)
                    self.chargeplan_handles.append(start_handle)
                    self.chargeplan_handles.append(end_handle)

        else:
            self.notify("Failed to create a chargeplan")

    def create_a_charge_plan(self):
        """Create a chargeplan"""
        self.log("called create_a_charge_plan", level="DEBUG")

        if self.get_state(self.args["smart_charging"], default="off") == "off":
            self.log("Smart charging is off")
            return False

        now = self.datetime(aware=True)
        ip_state = self.get_state(self.args["charger_ready_at"])
        ready_until = self.parse_datetime(ip_state, aware=True)

        if ready_until is not None and now > ready_until:
            ready_until = ready_until + timedelta(days=1)

        car_soc = float(
            self.get_state(self.args["car_battery_sensor_entity"], default=0)
        )
        car_kwh_battery = float(self.args["car_battery_size_kwh"])
        # Based on the onboard charger in the car and the charger.
        max_charge_speed = float(self.args.get("car_onboard_charger_kwh", 11.0))

        state = self.get_state(self.args["power_price_entity"], attribute="all")
        tomorrow = state.get("attributes", {}).get("raw_tomorrow", [])
        today = state.get("attributes", {}).get("raw_today", [])
        currency = state.get("attributes", {}).get("currency")
        possible_hours = today + tomorrow
        str_format = "%Y-%m-%dT%H:%M:%S%z"
        nor_str_format = "%d.%m.%Y %H:%M:%S"

        avail_hours = []
        for i in possible_hours:
            start = datetime.strptime(i["start"], str_format)
            end = datetime.strptime(i["end"], str_format)
            # Lets skip all hours that is already passed.
            if now > start:
                self.log(
                    "skipped %s bc hour is passed %s", start, i["value"], level="DEBUG"
                )
                continue
            elif ready_until is not None and start > ready_until:
                self.log(
                    "skipped %s  %s bc hour is after the car should be ready",
                    start,
                    i["value"],
                    level="DEBUG",
                )
                continue
            else:
                data = {"start": start, "end": end, "value": i["value"]}
                if i["value"] is not None:
                    avail_hours.append(data)

        if len(avail_hours):
            number_of_kwh_to_charge = car_kwh_battery - car_kwh_battery / 100 * car_soc
            numbers_of_hours_required_to_be_fully_charged = math.ceil(
                number_of_kwh_to_charge / max_charge_speed
            )

            self.log(
                "Need %s kwh hours %s to reach soc before %s",
                number_of_kwh_to_charge,
                numbers_of_hours_required_to_be_fully_charged,
                ready_until,
                level="INFO",
            )

            if (
                now + timedelta(hours=numbers_of_hours_required_to_be_fully_charged)
                > ready_until
            ):
                msg = "Can't charge to soc limit in the timeframe."
                self.notify(msg)
                self.log(msg, level="INFO")

            hours = list(sorted(avail_hours, key=itemgetter("value")))

            cheapest_hours = hours[:numbers_of_hours_required_to_be_fully_charged]

            nexp_hours = hours[numbers_of_hours_required_to_be_fully_charged:]

            # Just some logging for debugging.
            for cch in cheapest_hours:
                self.log(
                    "picked %s - %s price %s",
                    cch["start"],
                    cch["end"],
                    cch["value"],
                    level="DEBUG",
                )
            for exph in nexp_hours:
                self.log(
                    "skipped %s - %s price %s",
                    exph["start"],
                    exph["end"],
                    exph["value"],
                    level="DEBUG",
                )

            # Create a chargeplan with continues start and end as cars/chargers
            # dont like to get stopped/started
            chargeplan = get_continues_timespan(cheapest_hours)
            # For tesla is possible to get a sensor with time remaining until
            # soc in reached the charge limit.
            cost = 0

            msg = []
            for i, part_plan in enumerate(chargeplan):
                self.log(
                    "Charge should start at %s and end at %s",
                    part_plan[0],
                    part_plan[1],
                    level="DEBUG",
                )
                price_for_hour = []
                for ch in cheapest_hours:
                    if part_plan[0] <= ch["start"] and ch["end"] <= part_plan[1]:
                        self.log(
                            f"{part_plan[0]} {part_plan[1]} {ch['start']} {ch['end']} {ch['value']}",
                            level="DEBUG",
                        )
                        price_for_hour.append(ch["value"])
                        cost += float(ch["value"])

                msg.append(
                    f"{i+1}: {part_plan[0].strftime(nor_str_format)} - {part_plan[1].strftime(nor_str_format)} avg: {statistics.mean(price_for_hour)}"
                )

            self.notify("\n".join(msg), title="Created chargeplan")
            self.log("Total cost should be %s %s", cost, currency, level="DEBUG")

            self.charge_plan.clear()
            self.chargeplan = chargeplan
            if len(chargeplan):
                return True
            else:
                return False

        else:
            # This isn't enough time to get the car ready
            # this can happen in the user sets ready until and it can't reach soc
            # in that time frame.
            msg = (
                "Can't charge the car of the wanted soc within %s attemping to start the charging NOW"
                % ready_until
            )
            self.notify(msg)
            self.log(msg, level="INFO")
            self.charger_service(self.args["charger_service_start"], verify=False)

    def watt_to_amp(self, value):
        return float(value) / self.args["volt"] / math.sqrt(self.args["phase"])

    def amp_to_watt(self, value):
        return value * self.args["volt"] * math.sqrt(self.args["phase"])

    @app_lock
    def check_load(self, pw_state):
        """helper to check the load of the power usage and see if we need to limit the amp to the charger."""

        # I dont know why this is needed, seems to be some kind of race condition.
        esn = self.args["entity_start_name"]
        max_main_fuse_amps = self.args["main_fuse"] * 0.9
        total_usage_in_amps = self.watt_to_amp(pw_state)

        # How many amps are the charger using right now.
        charger_usage_amps = float(self.get_state("sensor.%s_in_current" % esn))
        # Max amps that can be used on the charger atm.
        dynamic_circuit_current_limit = float(
            self.get_state("sensor.%s_dynamic_circuit_current" % esn)
        )
        charger_current_max_amps = float(
            self.get_state("sensor.%s_max_circuit_current" % esn)
        )
        # charger_current_max_amps = 16.0

        house_usage_in_amps = total_usage_in_amps - charger_usage_amps
        # Right now we reserve 10% of the amps to create some kind of buffer
        # that might be to much you have small fuse and 400 v.
        # Make a config option.? # TODO
        amps_left = max_main_fuse_amps - house_usage_in_amps

        if self._loadbalancer_last_value is None:
            self._loadbalancer_last_value = 0  # dynamic_circuit_current_limit

        if math.floor(amps_left) == math.floor(self._loadbalancer_last_value):
            self.log(
                "Has correct amps limit amps left %s last value %s",
                amps_left,
                self._loadbalancer_last_value,
            )
            return

        if amps_left < self._loadbalancer_last_value:
            self.log(
                "Need to limit the charger as we only got %sA (%sW) available but the charger can use %sA",
                amps_left,
                pw_state,
                self._loadbalancer_last_value,
                level="DEBUG",
            )
            # This is only for the logging, the charger should be paused if the amp is less then 6
            # maybe check what error code is used.
            if amps_left < 6:
                self.log(
                    "Charger will paused because of amp %s < 6",
                    amps_left,
                    level="DEBUG",
                )
                self._charger_paused_by_loadbalance = True
                amps_left = 5
            new_amp_limit = min(amps_left, charger_current_max_amps)
            self.set_circuit_current_limit(new_amp_limit)
            self._loadbalancer_last_value = new_amp_limit

        elif amps_left > self._loadbalancer_last_value:
            new_amp_limit = min(amps_left, charger_current_max_amps)
            self.log(
                "Can increase the dynamic_circuit_current_limit %sA to current %sA",
                self._loadbalancer_last_value,
                new_amp_limit,
                level="DEBUG",
            )
            self.set_circuit_current_limit(new_amp_limit)
            self._loadbalancer_last_value = new_amp_limit

            if new_amp_limit > 6:
                self._charger_paused_by_loadbalance = False

        self.log("Charger watt %s from sensor", self.amp_to_watt(charger_usage_amps))
        self.log(
            "Using a total of %sA (%sW) amps house: %sA (%sW) main_fuse %sA charger: %sA (%sW) got %s left charger limit is %s fuse %s",
            total_usage_in_amps,
            self.amp_to_watt(total_usage_in_amps),
            house_usage_in_amps,
            self.amp_to_watt(house_usage_in_amps),
            self.args["main_fuse"],
            charger_usage_amps,
            self.amp_to_watt(charger_usage_amps),
            amps_left,
            self._loadbalancer_last_value,
            charger_current_max_amps,
            level="DEBUG",
        )

    def simulate(self, fra=None, til=None):
        """Grab the history, just do debugging, delete it later."""

        if fra is None:
            fra = "2020-10-24 00:00:00+02:00"
        if til is None:
            til = "2020-10-24 06:00:00+02:00"

        stop_at = datetime.fromisoformat(til)
        data = self.get_history(
            entity_id="sensor.mqtt_relay_energy_usage", start_time=fra
        )

        for d in data:
            for z in d:
                dt = datetime.fromisoformat(z["last_changed"])

                if dt > stop_at:
                    break
                else:
                    self.check_load(z["state"])

    def load_balance_cb(self, entity, attribute, old, new, kwargs):
        """Callback that use called when a new power usage is posted in ha."""
        # Quick check to see if need to compute anything.
        use_balance = self.get_state(self.args["load_balance"])
        charger_status = self.get_state(self.args["charger_status_entity"])
        # Cba loadbalance if the charger is idle.
        if charger_status in ("STANDBY", "PAUSED") or use_balance == "off":
            return
        if old != new:
            self.check_load(new)
