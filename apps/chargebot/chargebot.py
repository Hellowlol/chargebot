import math
import statistics
from datetime import datetime, timedelta
from operator import itemgetter

import hassapi as hass


"""
Chargebot

This is charge bot tailored for my use, this used a combination of easee, tesla and nordpool integration
in home assistant. It should be easy to change this to your usage.

Example config

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
        self.set_log_level(self.args.get("loglevel", "INFO"))
        # DUMP args.
        for arg in self.args:
            self.log("%s %s", arg, self.args[arg])
        # Chargeplans
        self.charge_plan = []
        # listener callbacks.
        self.app_callbacks = []
        # cancel timer callbacks.
        self.chargeplan_handles = []
        self._has_been_limited = None

        self.handle_cb_load_balance = self.listen_state(
            self.load_balance_cb, self.args["power_usage_in_w"]
        )

        # Cb for when the car is connceted to the charger.
        self.handle_cb_charge_plan = self.listen_state(
            self.chargeplan_cb,
            self.args["charger_status_entity"],
            old=self.args["charger_status_old"],
            new=self.args["charger_status_new"],
        )
        # Cb for when someone edits the ready_at shit.
        self.handle_cb_edit_ready_at = self.listen_state(
            self.charger_ready_at_cb, self.args["charger_ready_at"]
        )

        # Add some callbacks
        self.app_callbacks.append(self.handle_cb_charge_plan)
        self.app_callbacks.append(self.handle_cb_load_balance)
        self.app_callbacks.append(self.handle_cb_edit_ready_at)

        # Lets add this as a service so it can get
        # executed manually using the api
        self.register_service(
            "chargebot/create_a_charge_plan", self.reschedule_charge_plan
        )
        self.register_service("chargebot/cancel_change_plans", self.cancel_change_plans)

    def notify(self, message, **kwargs):
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

    def charger_service(self, data, verify=True):
        service = data.pop("service")
        kw = data.pop("data", {})
        call = None
        if self.args["verify_car_connected_and_home"] is True and verify is True:
            if self.verify_car() is True:
                call = self.call_service(service, **kw)
        else:
            call = self.call_service(service, **kw)

        if call is not None:
            self.notify("Sent charge service")
            self.log("Sent charge service with service: %s data: %s", service, kw)
            return call

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
        self.cancel_listen_state(self.handle_cb_edit_ready_at)
        self.cancel_change_plans()

    def charger_ready_at_cb(self, entity, attribute, old, new, kwargs):
        """Callback that executes when charger_ready_at is changed in
        home assistant. This cancels a chargeplan that is queued up
        and creates a new one.
        """
        self.log(
            f"charger_ready_at_cb executed {entity} {attribute} old: {old} new: {new} {kwargs}",
            level="DEBUG",
        )
        if old == new:
            self.log("same state..")
            return
        self.reschedule_charge_plan()

    def reschedule_charge_plan(self):
        """Reschedule and recreate a chargeplan."""
        self.log("Reschedule changeplans", level="DEBUG")
        self.cancel_change_plans()
        self.create_and_schedule_chargeplan()

    def cancel_change_plans(self):
        """Cancel all chargeplans and timers."""
        self.log("Canceling chargeplans", level="DEBUG")
        if len(self.chargeplan_handles):
            for handle in self.chargeplan_handles:
                self.cancel_timer(handle)
            self.chargeplan_handles.clear()

    def chargeplan_cb(self, entity, attribute, old, new, kwargs):
        self.log("chargeplan_cb", level="DEBUG")
        self.reschedule_charge_plan()

    def create_and_schedule_chargeplan(self):
        self.log("called _create_and_schedule_chargeplan", level="DEBUG")
        if self.create_a_charge_plan() is True:
            if len(self.chargeplan):
                for start, end in self.chargeplan:
                    self.log("Added starting charging at %s and stop at %s", start, end)

                    start_handle = self.run_at(
                        self.charger_service,
                        start,
                        **self.args["charger_service_start"],
                    )
                    end_handle = self.run_at(
                        self.charger_service,
                        end,
                        **self.args["charger_service_end"],
                    )
                    self.chargeplan_handles.append(start_handle)
                    self.chargeplan_handles.append(end_handle)

        else:
            self.log("No chargeplan")

    def create_a_charge_plan(self):
        """Create a chargeplan"""
        self.log("called create_a_charge_plan", level="DEBUG")

        if self.get_state(self.args["smart_charging"], default="off") == "off":
            self.log("smart_charing is off")
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
        max_charge_speed = float(self.args.get("charger_max_speed_kwh", 11.0))

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
                self.log("skipped %s bc hour is passed", start, level="DEBUG")
                continue
            elif ready_until is not None and start > ready_until:
                self.log(
                    "skipped %s cb hour is after the car should be ready",
                    start,
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
                "Need %s kwh hours %s",
                number_of_kwh_to_charge,
                numbers_of_hours_required_to_be_fully_charged,
                level="INFO",
            )

            if (
                now + timedelta(hours=numbers_of_hours_required_to_be_fully_charged)
                > ready_until
            ):
                msg = "Can't charge to soc limit in the timeframe."
                self.notify(msg)
                self.log(msg, level="INFO")

            cheapest_hours = sorted(avail_hours, key=itemgetter("value"))[
                :numbers_of_hours_required_to_be_fully_charged
            ]

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

    def load_balance_cb(self, entity, attribute, old, new, kwargs):
        """Callback used to manage the loadbalance"""
        if self.get_state(self.args["load_balance"], default="off") == "off":
            return

        pw_state = self.get_state(self.args["power_usage_in_w"])
        main_fuse_in_w = (
            self.args["main_fuse"] * self.args["volt"] * math.sqrt(self.args["phase"])
        )
        if float(pw_state) >= main_fuse_in_w:
            charger_state = self.get_state(self.args["charger_status_entity"])
            if charger_state == self.args["charger_status_charging"]:
                # it would be easier to just use toggle for this but some other
                # chargers might not support toggle.
                self.charger_service(self.args["charger_service_end"], verify=False)
                self._has_been_limited = True
                msg = "The charger was stopped/limited because the power usage is higher then main fuse"
                self.notify(msg)
                self.log(msg, level="INFO")
            else:
                self.log("Over limit but the charger isnt charging..", level="INFO")

        else:
            # Should add some kind of cool down here..
            # or atlease check vs the onboardcharger/max charger watt usage.
            if self._has_been_limited is True:
                self.log("Started charger again", level="INFO")
                self.charger_service(self.args["charger_service_start"], verify=False)
                self._has_been_limited = False
                self.notify(
                    "Started the charger was the power usage is less then main fuse"
                )
