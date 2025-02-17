"""Code for simulating the classic WoW feral cat DPS rotation."""

import numpy as np
import copy
import collections
import urllib
import multiprocessing
import psutil
import sim_utils
import player as player_class


class ArmorDebuffs():

    """Controls the delayed application of boss armor debuffs after an
    encounter begins. At present, only Sunder Armor and Expose Armor are
    modeled with delayed application, and all other boss debuffs are modeled
    as applying instantly at the fight start."""

    def __init__(self, sim):
        """Initialize controller by specifying whether Sunder, EA, or both will
        be applied.

        sim (Simulation): Simulation object controlling fight execution. The
            params dictionary of the Simulation will be modified by the debuff
            controller during the fight.
        """
        self.params = sim.params
        self.process_params()

    def process_params(self):
        """Use the simulation's existing params dictionary to determine whether
        Sunder, EA, or both should be applied."""
        self.use_sunder = bool(self.params['sunder'])
        self.reset()

    def reset(self):
        """Remove all armor debuffs at the start of a fight."""
        self.params['sunder'] = 0

    def update(self, time, player, sim):
        """Add Sunder or EA applications at the appropriate times. Currently,
        the debuff schedule is hard coded as 1 Sunder stack every GCD, and
        EA applied at 15 seconds if used. This can be made more flexible if
        desired in the future using class attributes.

        Arguments:
            time (float): Simulation time, in seconds.
            player (player.Player): Player object whose attributes will be
                modified by the trinket proc.
            sim (tbc_cat_sim.Simulation): Simulation object controlling the
                fight execution.
        """
        # If we are Sundering and are at less than 5 stacks, then add a stack
        # every GCD.
        if (self.use_sunder and (self.params['sunder'] < 5)
                and (time >= 1.5 * self.params['sunder'])):
            self.params['sunder'] += 1

            if sim.log:
                sim.combat_log.append(
                    sim.gen_log(time, 'Sunder Armor', 'applied')
                )

            player.calc_damage_params(**self.params)

        return 0.0


class UptimeTracker():

    """Provides an interface for tracking average uptime on buffs and debuffs,
    analogous to Trinket objects."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.uptime = 0.0
        self.last_update = 15.0
        self.active = False
        self.num_procs = 0

    def update(self, time, player, sim):
        """Update average aura uptime at a new timestep.

        Arguments:
            time (float): Simulation time, in seconds.
            player (player.Player): Player object responsible for
                ability casts.
            sim (wotlk_cat_sim.Simulation): Simulation object controlling the
                fight execution.
        """
        if (time > self.last_update) and (time < sim.fight_length - 15):
            dt = time - self.last_update
            active_now = self.is_active(player, sim)
            self.uptime = (
                (self.uptime * (self.last_update - 15.) + dt * active_now)
                / (time - 15.)
            )
            self.last_update = time

            if active_now and (not self.active):
                self.num_procs += 1

            self.active = active_now

        return 0.0

    def is_active(self, player, sim):
        """Determine whether or not the tracked aura is active at the current
        time. This method must be implemented by UptimeTracker subclasses.

        Arguments:
            player (wotlk_cat_sim.Player): Player object responsible for
                ability casts.
            sim (wotlk_cat_sim.Simulation): Simulation object controlling the
                fight execution.

        Returns:
            is_active (bool): Whether or not the aura is currently active.
        """
        return NotImplementedError(
            'Logic for aura active status must be implemented by UptimeTracker'
            ' subclasses.'
        )

    def deactivate(self, *args, **kwargs):
        self.active = False


class RipTracker(UptimeTracker):
    proc_name = 'Rip'

    def is_active(self, player, sim):
        return sim.rip_debuff


class RoarTracker(UptimeTracker):
    proc_name = 'Savage Roar'

    def is_active(self, player, sim):
        return player.savage_roar


class Simulation():

    """Sets up and runs a simulated fight with the cat DPS rotation."""

    # Default fight parameters, including boss armor and all relevant debuffs.
    default_params = {
        'gift_of_arthas': True,
        'boss_armor': 3731,
        'sunder': False,
        'faerie_fire': True,
        'blood_frenzy': False,
        'shattering_throw': False,
    }

    # Default parameters specifying the player execution strategy
    default_strategy = {
        'min_combos_for_rip': 5,
        'use_rake': False,
        'use_bite': True,
        'bite_time': 8.0,
        'min_combos_for_bite': 5,
        'mangle_spam': False,
        'bear_mangle': False,
        'use_berserk': False,
        'prepop_berserk': False,
        'preproc_omen': False,
        'bearweave': False,
        'berserk_bite_thresh': 100,
        'lacerate_prio': False,
        'lacerate_time': 10.0,
        'powerbear': False,
        'min_roar_offset': 10.0,
    }

    def __init__(
        self, player, fight_length, latency, trinkets=[], haste_multiplier=1.0,
        hot_uptime=0.0, **kwargs
    ):
        """Initialize simulation.

        Arguments:
            player (Player): An instantiated Player object which can execute
                the DPS rotation.
            fight_length (float): Fight length in seconds.
            latency (float): Modeled player input delay in seconds. Used to
                simulate realistic delays between energy gains and subsequent
                special ability casts, as well as delays in powershift timing
                relative to the GCD.
            trinkets (list of trinkets.Trinket): List of ActivatedTrinket or
                ProcTrinket objects that will be used on cooldown.
            haste_multiplier (float): Total multiplier from external percentage
                haste buffs such as Windfury Totem. Defaults to 1.
            hot_uptime (float): Fractional uptime of Rejuvenation / Wild Growth
                HoTs from a Restoration Druid. Used for simulating Revitalize
                procs. Defaults to 0.
            kwargs (dict): Key, value pairs for all other encounter parameters,
                including boss armor, relevant debuffs, and player stregy
                specification. An error will be thrown if the parameter is not
                recognized. Any parameters not supplied will be set to default
                values.
        """
        self.player = player
        self.fight_length = fight_length
        self.latency = latency
        self.trinkets = trinkets
        self.params = copy.deepcopy(self.default_params)
        self.strategy = copy.deepcopy(self.default_strategy)

        for key, value in kwargs.items():
            if key in self.params:
                self.params[key] = value
            elif key in self.strategy:
                self.strategy[key] = value
            else:
                raise KeyError(
                    ('"%s" is not a supported parameter. Supported encounter '
                     'parameters are: %s. Supported strategy parameters are: '
                     '%s.') % (key, self.params.keys(), self.strategy.keys())
                )

        # Set up controller for delayed armor debuffs. The controller can be
        # treated identically to a Trinket object as far as the sim is
        # concerned.
        self.debuff_controller = ArmorDebuffs(self)
        self.trinkets.append(self.debuff_controller)

        # Set up trackers for Rip and Roar uptime
        self.trinkets.append(RipTracker())
        self.trinkets.append(RoarTracker())

        # Calculate damage ranges for player abilities under the given
        # encounter parameters.
        self.player.calc_damage_params(**self.params)

        # Set multiplicative haste buffs. The multiplier can be increased
        # during Bloodlust, etc.
        self.haste_multiplier = haste_multiplier

        # Calculate time interval between Revitalize dice rolls
        self.revitalize_frequency = 15. / (8 * max(hot_uptime, 1e-9))

    def set_active_debuffs(self, debuff_list):
        """Set active debuffs according to a specified list.

        Arguments:
            debuff_list (list): List of strings containing supported debuff
                names.
        """
        active_debuffs = copy.copy(debuff_list)
        all_debuffs = [key for key in self.params if key != 'boss_armor']

        for key in all_debuffs:
            if key in active_debuffs:
                self.params[key] = True
                active_debuffs.remove(key)
            else:
                self.params[key] = False

        if active_debuffs:
            raise ValueError(
                'Unsupported debuffs found: %s. Supported debuffs are: %s.' % (
                    active_debuffs, self.params.keys()
                )
            )

        self.debuff_controller.process_params()

    def gen_log(self, time, event, outcome):
        """Generate a custom combat log entry.

        Arguments:
            time (float): Current simulation time in seconds.
            event (str): First "event" field for the log entry.
            outcome (str): Second "outcome" field for the log entry.
        """
        return [
            '%.3f' % time, event, outcome, '%.1f' % self.player.energy,
            '%d' % self.player.combo_points, '%d' % self.player.mana,
            '%d' % self.player.rage
        ]

    def mangle(self, time):
        """Instruct the Player to Mangle, and perform related bookkeeping.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            damage_done (float): Damage done by the Mangle cast.
        """
        damage_done, success = self.player.mangle()

        # If it landed, flag the debuff as active and start timer
        if success:
            self.mangle_debuff = True
            self.mangle_end = (
                np.inf if self.strategy['bear_mangle'] else (time + 60.0)
            )

        return damage_done

    def rake(self, time):
        """Instruct the Player to Rake, and perform related bookkeeping.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            damage_done (float): Damage done by the Rake initial hit.
        """
        damage_done, success = self.player.rake(self.mangle_debuff)

        # If it landed, flag the debuff as active and start timer
        if success:
            self.rake_debuff = True
            self.rake_end = time + 9.0
            self.rake_ticks = list(np.arange(time + 3, time + 9.01, 3))
            self.rake_damage = self.player.rake_tick
            self.rake_sr_snapshot = self.player.savage_roar

        return damage_done

    def lacerate(self, time):
        """Instruct the Player to Lacerate, and perform related bookkeeping.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            damage_done (float): Damage done by the Lacerate initial hit.
        """
        damage_done, success = self.player.lacerate(self.mangle_debuff)

        if success:
            self.lacerate_end = time + 15.0

            if self.lacerate_debuff:
                # Unlike our other bleeds, Lacerate maintains its tick rate
                # when it is refreshed, so we simply append more ticks to
                # extend the duration. Note that the current implementation
                # allows for Lacerate to be refreshed *after* the final tick
                # goes out as long as it happens before the duration expires.
                if self.lacerate_ticks:
                    last_tick = self.lacerate_ticks[-1]
                else:
                    last_tick = self.last_lacerate_tick

                self.lacerate_ticks += list(np.arange(
                    last_tick + 3, self.lacerate_end + 1e-9, 3
                ))
                self.lacerate_stacks = min(self.lacerate_stacks + 1, 5)
            else:
                self.lacerate_debuff = True
                self.lacerate_ticks = list(np.arange(time + 3, time + 16, 3))
                self.lacerate_stacks = 1

            self.lacerate_damage = (
                self.player.lacerate_tick * self.lacerate_stacks
                * (1 + 0.15 * self.player.enrage)
            )
            self.lacerate_crit_chance = self.player.crit_chance - 0.04

        return damage_done

    def rip(self, time):
        """Instruct Player to apply Rip, and perform related bookkeeping.

        Arguments:
            time (float): Current simulation time in seconds.
        """
        damage_per_tick, success = self.player.rip()

        if success:
            self.rip_debuff = True
            self.rip_start = time
            self.rip_end = time + self.player.rip_duration
            self.rip_ticks = list(np.arange(time + 2, self.rip_end + 1e-9, 2))
            self.rip_damage = damage_per_tick
            self.rip_crit_chance = self.player.crit_chance
            self.rip_sr_snapshot = self.player.savage_roar

        return 0.0

    def shred(self):
        """Instruct Player to Shred, and perform related bookkeeping.

        Returns:
            damage_done (Float): Damage done by Shred cast.
        """
        damage_done, success = self.player.shred(self.mangle_debuff)

        # If it landed, apply Glyph of Shred
        if success and self.rip_debuff and self.player.shred_glyph:
            if (self.rip_end - self.rip_start) < self.player.rip_duration + 6:
                self.rip_end += 2
                self.rip_ticks.append(self.rip_end)

        return damage_done

    def berserk_expected_at(self, current_time, future_time):
        """Determine whether the Berserk buff is predicted to be active at
        the requested future time.

        Arguments:
            current_time (float): Current simulation time in seconds.
            future_time (float): Future time, in seconds, for querying Berserk
                status.

        Returns:
            berserk_expected (bool): True if Berserk should be active at the
                specified future time, False otherwise.
        """
        if self.player.berserk:
            return (
                (future_time < self.berserk_end)
                or (future_time > current_time + self.player.berserk_cd)
            )
        if self.player.berserk_cd > 1e-9:
            return (future_time > current_time + self.player.berserk_cd)
        if self.params['tigers_fury'] and self.strategy['use_berserk']:
            return (future_time > self.tf_end)
        return False

    def tf_expected_before(self, current_time, future_time):
        """Determine whether Tiger's Fury is predicted to be used prior to the
        requested future time.

        Arguments:
            current_time (float): Current simulation time in seconds.
            future_time (float): Future time, in seconds, for querying Tiger's
                Fury status.

        Returns:
            tf_expected (bool): True if Tiger's Fury should be activated prior
                to the specified future time, False otherwise.
        """
        if self.player.tf_cd > 1e-9:
            return (current_time + self.player.tf_cd < future_time)
        if self.player.berserk:
            return (self.berserk_end < future_time)
        return True

    def can_bite(self, time):
        """Determine whether or not there is sufficient time left before Rip
        falls off to fit in a Ferocious Bite. Uses either a fixed empirically
        optimized time parameter or a first principles analytical calculation
        depending on user options.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            can_bite (bool): True if Biting now is optimal.
        """
        if self.strategy['bite_time'] is not None:
            return (
                (self.rip_end - time >= self.strategy['bite_time'])
                and (self.roar_end - time >= self.strategy['bite_time'])
            )
        return self.can_bite_analytical(time)

    def can_bite_analytical(self, time):
        """Analytical alternative to the empirical bite_time parameter used for
        determining whether there is sufficient time left before Rip falls off
        to fit in a Ferocious Bite.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            can_bite (bool): True if the analytical model indicates that Biting
                now is optimal, False otherwise.
        """
        # First calculate how much Energy we expect to accumulate before our
        # next finisher expires.
        maxripdur = self.player.rip_duration + 6 * self.player.shred_glyph
        ripdur = self.rip_start + maxripdur - time
        srdur = self.roar_end - time
        mindur = min(ripdur, srdur)
        maxdur = max(ripdur, srdur)
        expected_energy_gain_min = 10 * mindur
        expected_energy_gain_max = 10 * maxdur

        if self.tf_expected_before(time, time + mindur):
            expected_energy_gain_min += 60
        if self.tf_expected_before(time, time + maxdur):
            expected_energy_gain_max += 60

        if self.player.omen:
            expected_energy_gain_min += mindur / self.swing_timer * (
                3.5 / 60. * (1 - self.player.miss_chance) * 42
            )
            expected_energy_gain_max += maxdur / self.swing_timer * (
                3.5 / 60. * (1 - self.player.miss_chance) * 42
            )

        expected_energy_gain_min += mindur/self.revitalize_frequency*0.15*8
        expected_energy_gain_max += maxdur/self.revitalize_frequency*0.15*8

        total_energy_min = self.player.energy + expected_energy_gain_min
        total_energy_max = self.player.energy + expected_energy_gain_max

        # Now calculate the effective Energy cost for Biting now, which
        # includes the cost of the Ferocious Bite itself, the cost of building
        # CPs for Rip and Roar, and the cost of Rip/Roar.
        ripcost, bitecost, srcost = self.get_finisher_costs(time)
        cp_per_builder = 1 + self.player.crit_chance
        cost_per_builder = (
            (42. + 42. + 35.) / 3. * (1 + 0.2 * self.player.miss_chance)
        )

        if srdur < ripdur:
            nextcost = srcost
            secondcps = 5
        else:
            nextcost = ripcost
            secondcps = 1

        total_energy_cost_min = (
            bitecost + 5. / cp_per_builder * cost_per_builder + nextcost
        )
        total_energy_cost_max = (
            bitecost + (5. + secondcps) / cp_per_builder * cost_per_builder
            + ripcost + srcost
        )

        # Actual Energy cost is a bit lower than this because it is okay to
        # lose a few seconds of Rip or SR uptime to gain a Bite.
        rip_downtime, sr_downtime = self.calc_allowed_rip_downtime(time)

        # Adjust downtime estimate to account for end of fight losses
        rip_downtime = maxripdur * (1 - 1. / (1. + rip_downtime / maxripdur))
        sr_downtime = 34. * (1 - 1. / (1. + sr_downtime / 34.))
        next_downtime = sr_downtime if srdur < ripdur else rip_downtime

        total_energy_cost_min -= 10 * next_downtime
        total_energy_cost_max -= 10 * min(rip_downtime, sr_downtime)

        # Then we simply recommend Biting now if the available Energy to do so
        # exceeds the effective cost.
        return (
            (total_energy_min > total_energy_cost_min)
            and (total_energy_max > total_energy_cost_max)
        )

    def get_finisher_costs(self, time):
        """Determine the expected Energy cost for Rip when it needs to be
        refreshed, and the expected Energy cost for Ferocious Bite if it is
        cast right now.

        Arguments:
            time (float): Current simulation time, in seconds.

        Returns:
            ripcost (float): Energy cost of future Rip refresh.
            bitecost (float): Energy cost of a current Ferocious Bite cast.
            srcost (float): Energy cost of a Savage Roar refresh.
        """
        rip_end = time if (not self.rip_debuff) else self.rip_end
        ripcost = 15 if self.berserk_expected_at(time, rip_end) else 30

        if self.player.energy >= self.player.bite_cost:
            bitecost = min(self.player.bite_cost + 30, self.player.energy)
        else:
            bitecost = self.player.bite_cost + 10 * self.latency

        sr_end = time if (not self.player.savage_roar) else self.roar_end
        srcost = 12.5 if self.berserk_expected_at(time, sr_end) else 25

        return ripcost, bitecost, srcost

    def calc_allowed_rip_downtime(self, time):
        """Determine how many seconds of Rip uptime can be lost in exchange for
        a Ferocious Bite cast without losing damage. This calculation is used
        in the analytical bite_time calculation above, as well as for
        determining how close to the end of the fight we should be for
        prioritizing Bite over Rip.

        Arguments:
            time (float): Current simulation time, in seconds.

        Returns:
            allowed_rip_downtime (float): Maximum acceptable Rip duration loss,
                in seconds.
            allowed_sr_downtime (float): Maximum acceptable Savage Roar
                downtime, in seconds.
        """
        rip_cp = self.strategy['min_combos_for_rip']
        bite_cp = self.strategy['min_combos_for_bite']
        rip_cost, bite_cost, roar_cost = self.get_finisher_costs(time)
        crit_factor = self.player.calc_crit_multiplier() - 1
        bite_base_dmg = 0.5 * (
            self.player.bite_low[bite_cp] + self.player.bite_high[bite_cp]
        )
        bite_bonus_dmg = (
            (bite_cost - self.player.bite_cost)
            * (9.4 + self.player.attack_power / 410.)
            * self.player.bite_multiplier
        )
        bite_dpc = (bite_base_dmg + bite_bonus_dmg) * (
            1 + crit_factor * (self.player.crit_chance + 0.25)
        )
        crit_mod = crit_factor * self.player.crit_chance
        avg_rip_tick = self.player.rip_tick[rip_cp] * 1.3 * (
            1 + crit_mod * self.player.primal_gore
        )
        shred_dpc = (
            0.5 * (self.player.shred_low + self.player.shred_high) * 1.3
            * (1 + crit_mod)
        )
        allowed_rip_downtime = (
            (bite_dpc - (bite_cost - rip_cost) * shred_dpc / 42.)
            / avg_rip_tick * 2
        )
        cpe = (42. * bite_dpc / shred_dpc - 35.) / 5.
        srep = {1: (1 - 5) * (cpe - 125./34.), 2: (2 - 5) * (cpe - 125./34.)}
        srep_avg = (
            self.player.crit_chance * srep[2]
            + (1 - self.player.crit_chance) * srep[1]
        )
        rake_dpc = 1.3 * (
            self.player.rake_hit * (1 + crit_mod)
            + 3*self.player.rake_tick*(1 + crit_mod*self.player.primal_gore)
        )
        allowed_sr_downtime = (
            (bite_dpc - shred_dpc / 42. * min(srep_avg, srep[1], srep[2]))
            / (0.33/1.33 * rake_dpc)
        )
        return allowed_rip_downtime, allowed_sr_downtime

    def clip_roar(self, time):
        """Determine whether to clip a currently active Savage Roar in order to
        de-sync the Rip and Roar timers.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            can_roar (bool): Whether or not to clip Roar now.
        """
        if (not self.rip_debuff) or (self.fight_length - self.rip_end < 10):
            return False

        # Project Rip end time assuming full Glyph of Shred extensions.
        max_rip_dur = self.player.rip_duration + 6 * self.player.shred_glyph
        rip_end = self.rip_start + max_rip_dur

        # If the existing Roar already falls off well after the existing Roar,
        # then no need to clip.
        # if self.roar_end >= rip_end + self.strategy['min_roar_offset']:
        if self.roar_end > rip_end:
            return False

        # Calculate when Roar would end if we cast it now.
        new_roar_dur = self.player.roar_durations[self.player.combo_points]
        new_roar_end = time + new_roar_dur

        # Clip as soon as we have enough CPs for the new Roar to expire well
        # after the current Rip.
        return (new_roar_end >= rip_end + self.strategy['min_roar_offset'])

    # def clip_roar(self, time):
    #     """Determine whether to clip a currently active Savage Roar in order to
    #     de-sync the Rip and Roar timers.

    #     Arguments:
    #         time (float): Current simulation time in seconds.

    #     Returns:
    #         can_roar (bool): Whether or not to clip Roar now.
    #     """
    #     # For now, consider only the case where Rip will expire after Roar
    #     if ((not self.rip_debuff) or (self.rip_end <= self.roar_end)
    #             or (self.fight_length - self.rip_end < 10)):
    #         return False

    #     # Calculate how much Energy we expect to accumulate after Roar expires
    #     # but before Rip expires.
    #     maxripdur = self.player.rip_duration + 6 * self.player.shred_glyph
    #     ripdur = self.rip_start + maxripdur - time
    #     roardur = self.roar_end - time
    #     available_time = ripdur - roardur
    #     expected_energy_gain = 10 * available_time

    #     if self.tf_expected_before(time, self.rip_end):
    #         expected_energy_gain += 60
    #     if self.player.omen:
    #         expected_energy_gain += available_time / self.swing_timer * (
    #             3.5 / 60. * (1 - self.player.miss_chance) * 42
    #         )
    #     if self.player.omen_proc:
    #         expected_energy_gain += 42

    #     expected_energy_gain += (
    #         available_time / self.revitalize_frequency * 0.15 * 8
    #     )

    #     # Add current Energy minus cost of Roaring now
    #     roarcost = 12.5 if self.player.berserk else 25
    #     available_energy = self.player.energy - roarcost + expected_energy_gain

    #     # Now calculate the effective Energy cost for building back 5 CPs once
    #     # Roar expires and casting Rip
    #     ripcost = 15 if self.berserk_expected_at(time, self.rip_end) else 30
    #     cp_per_builder = 1 + self.player.crit_chance
    #     cost_per_builder = (
    #         (42. + 42. + 35.) / 3. * (1 + 0.2 * self.player.miss_chance)
    #     )
    #     rip_refresh_cost = 5. / cp_per_builder * cost_per_builder + ripcost

    #     # If the cost is less than the expected Energy gain in the available
    #     # time, then there's no reason to clip Roar.
    #     if available_energy >= rip_refresh_cost:
    #         return False

    #     # On the other hand, if there is a time conflict, then use the
    #     # empirical parameter for how much we're willing to clip Roar.
    #     return roardur <= self.strategy['max_roar_clip']
    #     # return True

    def execute_rotation(self, time):
        """Execute the next player action in the DPS rotation according to the
        specified player strategy in the simulation.

        Arguments:
            time (float): Current simulation time in seconds.

        Returns:
            damage_done (float): Damage done by the player action.
        """
        # If we're out of form because we just cast GotW/etc., always shift
        #if not self.player.cat_form:
        #    self.player.shift(time)
        #    return 0.0

        # If we previously decided to shift, then execute the shift now once
        # the input delay is over.
        if self.player.ready_to_shift:
            self.player.shift(time)

            if (self.player.mana < 0) and (not self.time_to_oom):
                self.time_to_oom = time

            # Swing timer only updates on the next swing after we shift
            swing_fac = 1/2.5 if self.player.cat_form else 2.5
            self.update_swing_times(
                self.swing_times[0], self.swing_timer * swing_fac,
                first_swing=True
            )
            return 0.0

        energy, cp = self.player.energy, self.player.combo_points
        rip_cp = self.strategy['min_combos_for_rip']
        bite_cp = self.strategy['min_combos_for_bite']

        # 10/6/21 - Added logic to not cast Rip if we're near the end of the
        # fight.
        end_thresh = 10
        # end_thresh = self.calc_allowed_rip_downtime(time)
        rip_now = (
            (cp >= rip_cp) and (not self.rip_debuff)
            and (self.fight_length - time >= end_thresh)
            and (not self.player.omen_proc)
        )
        bite_at_end = (
            (cp >= bite_cp)
            and ((self.fight_length - time < end_thresh) or (
                    self.rip_debuff and
                    (self.fight_length - self.rip_end < end_thresh)
                )
            )
        )

        mangle_now = (
            (not rip_now) and (not self.mangle_debuff)
            # and (not self.player.omen_proc)
        )
        mangle_cost = self.player.mangle_cost

        bite_before_rip = (
            (cp >= bite_cp) and self.rip_debuff and self.player.savage_roar
            and self.strategy['use_bite'] and self.can_bite(time)
        )
        bite_now = (
            (bite_before_rip or bite_at_end)
            and (not self.player.omen_proc)
        )

        # During Berserk, we additionally add an Energy constraint on Bite
        # usage to maximize the total Energy expenditure we can get.
        if bite_now and self.player.berserk:
            bite_now = (energy <= self.strategy['berserk_bite_thresh'])

        rake_now = (
            (self.strategy['use_rake']) and (not self.rake_debuff)
            and (self.fight_length - time > 9)
            and (not self.player.omen_proc)
        )

        berserk_energy_thresh = 90 - 10 * self.player.omen_proc
        berserk_now = (
            self.strategy['use_berserk'] and (self.player.berserk_cd < 1e-9)
            and (self.player.tf_cd > 15 + 5 * self.player.berserk_glyph)
            # and (energy < berserk_energy_thresh + 1e-9)
        )

        # roar_now = (not self.player.savage_roar) and (cp >= 1)
        # pool_for_roar = (not roar_now) and (cp >= 1) and self.clip_roar(time)
        roar_now = (cp >= 1) and (
            (not self.player.savage_roar) or self.clip_roar(time)
        )

        # First figure out how much Energy we must float in order to be able
        # to refresh our buffs/debuffs as soon as they fall off
        pending_actions = []
        rip_refresh_pending = False

        if self.rip_debuff and (self.rip_end < self.fight_length - end_thresh):
            if self.berserk_expected_at(time, self.rip_end):
                rip_cost = 15
            else:
                rip_cost = 30

            pending_actions.append((self.rip_end, rip_cost))
            rip_refresh_pending = True
        if self.rake_debuff and (self.rake_end < self.fight_length - 9):
            if self.berserk_expected_at(time, self.rake_end):
                pending_actions.append((self.rake_end, 17.5))
            else:
                pending_actions.append((self.rake_end, 35))
        if self.mangle_debuff and (self.mangle_end < self.fight_length - 1):
            base_cost = self.player._mangle_cost
            if self.berserk_expected_at(time, self.mangle_end):
                pending_actions.append((self.mangle_end, 0.5 * base_cost))
            else:
                pending_actions.append((self.mangle_end, base_cost))
        if self.player.savage_roar:
            if self.berserk_expected_at(time, self.roar_end):
                pending_actions.append((self.roar_end, 12.5))
            else:
                pending_actions.append((self.roar_end, 25))

        pending_actions.sort()

        # Allow for bearweaving if the next pending action is >= 4.5s away
        furor_cap = min(20 * self.player.furor, 85)
        # weave_energy = min(furor_cap - 30 - 20 * self.latency, 42)
        weave_energy = furor_cap - 30 - 20 * self.latency

        if self.player.furor > 3:
            weave_energy -= 15

        weave_end = time + 4.5 + 2 * self.latency
        bearweave_now = (
            self.strategy['bearweave'] and (energy <= weave_energy)
            and (not self.player.omen_proc) and
            # ((not pending_actions) or (pending_actions[0][0] >= weave_end))
            ((not rip_refresh_pending) or (self.rip_end >= weave_end))
            # and (not self.tf_expected_before(time, weave_end))
            # and (not self.params['tigers_fury'])
            and (not self.player.berserk)
        )

        if bearweave_now and (not self.strategy['lacerate_prio']):
            bearweave_now = not self.tf_expected_before(time, weave_end)

        # If we're maintaining Lacerate, then allow for emergency bearweaves
        # if Lacerate is about to fall off even if the above conditions do not
        # apply.
        emergency_bearweave = (
            self.strategy['bearweave'] and self.strategy['lacerate_prio']
            and self.lacerate_debuff
            and (self.lacerate_end - time < 2.5 + self.latency)
            and (self.lacerate_end < self.fight_length)
        )

        floating_energy = 0
        previous_time = time
        #tf_pending = False

        for refresh_time, refresh_cost in pending_actions:
            delta_t = refresh_time - previous_time

            # if (not tf_pending):
            #     tf_pending = self.tf_expected_before(time, refresh_time)

            #     if tf_pending:
            #         refresh_cost -= 60

            if delta_t < refresh_cost / 10.:
                floating_energy += refresh_cost - 10 * delta_t
                previous_time = refresh_time
            else:
                previous_time += refresh_cost / 10.

        excess_e = energy - floating_energy
        time_to_next_action = 0.0

        if not self.player.cat_form:
            # Shift back into Cat Form if (a) our first bear auto procced
            # Clearcasting, or (b) our first bear auto didn't generate enough
            # Rage to Mangle or Maul, or (c) we don't have enough time or
            # Energy leeway to spend an additional GCD in Dire Bear Form.
            shift_now = (
                (energy + 15 + 10 * self.latency > furor_cap)
                or (rip_refresh_pending and (self.rip_end < time + 3.0))
            )
            shift_next = (
                (energy + 30 + 10 * self.latency > furor_cap)
                or (rip_refresh_pending and (self.rip_end < time + 4.5))
            )

            if self.strategy['powerbear']:
                powerbear_now = (not shift_now) and (self.player.rage < 10)
            else:
                powerbear_now = False
                shift_now = shift_now or (self.player.rage < 10)

            # lacerate_now = self.strategy['lacerate_prio'] and (
            #     (not self.lacerate_debuff) or (self.lacerate_stacks < 5)
            #     or (self.lacerate_end - time <= self.strategy['lacerate_time'])
            # )
            build_lacerate = (
                (not self.lacerate_debuff) or (self.lacerate_stacks < 5)
            )
            maintain_lacerate = (not build_lacerate) and (
                (self.lacerate_end - time <= self.strategy['lacerate_time'])
                and ((self.player.rage < 38) or shift_next)
                and (self.lacerate_end < self.fight_length)
            )
            lacerate_now = (
                self.strategy['lacerate_prio']
                and (build_lacerate or maintain_lacerate)
            )
            emergency_lacerate = (
                self.strategy['lacerate_prio'] and self.lacerate_debuff
                and (self.lacerate_end - time < 3.0 + 2 * self.latency)
                and (self.lacerate_end < self.fight_length)
            )

            if (not self.strategy['lacerate_prio']) or (not lacerate_now):
                shift_now = shift_now or self.player.omen_proc

            if emergency_lacerate and (self.player.rage >= 13):
                return self.lacerate(time)
            elif shift_now:
                self.player.ready_to_shift = True
            elif powerbear_now:
                self.player.shift(time, powershift=True)
            elif lacerate_now and (self.player.rage >= 13):
                return self.lacerate(time)
            elif (self.player.rage >= 15) and (self.player.mangle_cd < 1e-9):
                return self.mangle(time)
            elif self.player.rage >= 13:
                return self.lacerate(time)
            else:
                time_to_next_action = self.swing_times[0] - time
        elif emergency_bearweave:
            self.player.ready_to_shift = True
        elif berserk_now:
            self.apply_berserk(time)
            return 0.0
        elif roar_now: # or pool_for_roar:
            # If we have leeway to do so, don't Roar right away and instead
            # pool Energy to reduce how much we clip the buff
            # if pool_for_roar:
            #     roar_now = (
            #         (self.roar_end - time <= self.strategy['max_roar_clip'])
            #         or self.player.omen_proc or (energy >= 90)
            #     )

            # if not roar_now:
            #     time_to_next_action = min(
            #         self.roar_end - self.strategy['max_roar_clip'] - time,
            #         (90. - energy) / 10.
            #     )
            if energy >= self.player.roar_cost:
                self.roar_end = self.player.roar(time)
                return 0.0
            else:
                time_to_next_action = (self.player.roar_cost - energy) / 10.
        elif rip_now:
            if (energy >= self.player.rip_cost) or self.player.omen_proc:
                return self.rip(time)
            time_to_next_action = (self.player.rip_cost - energy) / 10.
        elif bite_now:
            if energy >= self.player.bite_cost:
                return self.player.bite()
            time_to_next_action = (self.player.bite_cost - energy) / 10.
        elif rake_now:
            if (energy >= self.player.rake_cost) or self.player.omen_proc:
                return self.rake(time)
            time_to_next_action = (self.player.rake_cost - energy) / 10.
        elif mangle_now:
            if (energy >= mangle_cost) or self.player.omen_proc:
                return self.mangle(time)
            time_to_next_action = (mangle_cost - energy) / 10.
        elif bearweave_now:
            self.player.ready_to_shift = True
        elif self.strategy['mangle_spam'] and (not self.player.omen_proc):
            if excess_e >= mangle_cost:
                return self.mangle(time)
            time_to_next_action = (mangle_cost - excess_e) / 10.
        else:
            if (excess_e >= self.player.shred_cost) or self.player.omen_proc:
                return self.shred()
            time_to_next_action = (self.player.shred_cost - excess_e) / 10.

        # Model in latency when waiting on Energy for our next action
        next_action = time + time_to_next_action

        if pending_actions:
            next_action = min(next_action, pending_actions[0][0])

        self.next_action = next_action + self.latency

        return 0.0

    def update_swing_times(self, time, new_swing_timer, first_swing=False):
        """Generate an updated list of swing times after changes to the swing
        timer have occurred.

        Arguments:
            time (float): Simulation time at which swing timer is changing, in
                seconds.
            new_swing_timer (float): Updated swing timer.
            first_swing (bool): If True, generate a fresh set of swing times
                at the start of a simulation. Defaults False.
        """
        # First calculate the start time for the next swing.
        if first_swing:
            start_time = time
        else:
            frac_remaining = (self.swing_times[0] - time) / self.swing_timer
            start_time = time + frac_remaining * new_swing_timer

        # Now update the internal swing times
        self.swing_timer = new_swing_timer

        if start_time > self.fight_length - self.swing_timer:
            self.swing_times = [
                start_time, start_time + self.swing_timer
            ]
        else:
            self.swing_times = list(np.arange(
                start_time, self.fight_length + self.swing_timer,
                self.swing_timer
            ))

    def apply_haste_buff(self, time, haste_rating_increment):
        """Perform associated bookkeeping when the player Haste Rating is
        modified.

        Arguments:
            time (float): Simulation time in seconds.
            haste_rating_increment (int): Amount by which the player Haste
                Rating changes.
        """
        new_swing_timer = sim_utils.calc_swing_timer(
            sim_utils.calc_haste_rating(
                self.swing_timer, multiplier=self.haste_multiplier,
                cat_form=self.player.cat_form
            ) + haste_rating_increment,
            multiplier=self.haste_multiplier, cat_form=self.player.cat_form
        )
        self.update_swing_times(time, new_swing_timer)

    def apply_tigers_fury(self, time):
        """Apply Tiger's Fury buff and document if requested.

        Arguments:
            time (float): Simulation time when Tiger's Fury is cast, in
                seconds
        """
        self.player.energy = min(100, self.player.energy + 60)
        self.params['tigers_fury'] = True
        self.player.calc_damage_params(**self.params)
        self.tf_end = time + 6.
        self.player.tf_cd = 30.
        self.next_action = time + self.latency
        self.proc_end_times.append(time + 30.)
        self.proc_end_times.sort()

        if self.log:
            self.combat_log.append(
                self.gen_log(time, "Tiger's Fury", 'applied')
            )

    def drop_tigers_fury(self, time):
        """Remove Tiger's Fury buff and document if requested.

        Arguments:
            time (float): Simulation time when Tiger's Fury fell off, in
                seconds. Used only for logging.
        """
        self.params['tigers_fury'] = False
        self.player.calc_damage_params(**self.params)

        if self.log:
            self.combat_log.append(
                self.gen_log(time, "Tiger's Fury", 'falls off')
            )

    def apply_berserk(self, time, prepop=False):
        """Apply Berserk buff and document if requested.

        Arguments:
            time (float): Simulation time when Berserk is cast, in seconds.
            prepop (bool): Whether Berserk is pre-popped 1 second before the
                start of combat rather than in the middle of the fight.
                Defaults False.
        """
        self.player.berserk = True
        self.player.set_ability_costs()
        self.player.gcd = 1.0 * (not prepop)
        self.berserk_end = time + 15. + 5 * self.player.berserk_glyph
        self.player.berserk_cd = 180. - prepop

        if self.log:
            self.combat_log.append(
                self.gen_log(time, 'Berserk', 'applied')
            )

        # if self.params['tigers_fury']:
        #     self.drop_tigers_fury(time)

    def drop_berserk(self, time):
        """Remove Berserk buff and document if requested.

        Arguments:
            time (float): Simulation time when Berserk fell off, in seconds.
                Used only for logging.
        """
        self.player.berserk = False
        self.player.set_ability_costs()

        if self.log:
            self.combat_log.append(
                self.gen_log(time, 'Berserk', 'falls off')
            )

    def apply_bleed_damage(
        self, base_tick_damage, crit_chance, ability_name, sr_snapshot, time
    ):
        """Apply a periodic damage tick from an active bleed effect.

        Arguments:
            base_tick_damage (float): Damage per tick of the bleed prior to
                Mangle or Savage Roar modifiers.
            crit_chance (float): Snapshotted critical strike chance of the
                bleed, between 0 and 1.
            ability_name (str): Name of the bleed ability. Used for combat
                logging.
            sr_snapshot (bool): Whether Savage Roar was active when the bleed
                was initially cast.
            time (float): Simulation time, in seconds. Used for combat logging.

        Returns:
            tick_damage (float): Final damage done by the bleed tick.
        """
        tick_damage = base_tick_damage * (1 + 0.3 * self.mangle_debuff)

        if (crit_chance > 0) and self.player.primal_gore:
            tick_damage, _, _ = sim_utils.calc_yellow_damage(
                tick_damage, tick_damage, 0.0, crit_chance,
                crit_multiplier=self.player.calc_crit_multiplier()
            )

        self.player.dmg_breakdown[ability_name]['damage'] += tick_damage

        if sr_snapshot:
            self.player.dmg_breakdown['Savage Roar']['damage'] += (
                self.player.roar_fac * tick_damage
            )
            tick_damage *= 1 + self.player.roar_fac

        if self.log:
            self.combat_log.append(
                self.gen_log(time, ability_name + ' tick', '%d' % tick_damage)
            )

        # Since a handful of proc effects trigger only on periodic damage, we
        # separately check for those procs here.
        for trinket in self.player.proc_trinkets:
            if trinket.periodic_only:
                trinket.check_for_proc(False, True)
                tick_damage += trinket.update(time, self.player, self)

        return tick_damage

    def run(self, log=False):
        """Run a simulated trajectory for the fight.

        Arguments:
            log (bool): If True, generate a full combat log of events within
                the simulation. Defaults False.

        Returns:
            times, damage, energy, combos: Lists of the time,
                total damage done, player energy, and player combo points at
                each simulated event within the fight duration.
            damage_breakdown (collection.OrderedDict): Dictionary containing a
                breakdown of the number of casts and damage done by each player
                ability.
            aura_stats (list of lists): Breakdown of the number of activations
                and total uptime of each buff aura applied from trinkets and
                other cooldowns.
            combat_log (list of lists): Each entry is a list [time, event,
                outcome, energy, combo points, mana] all formatted as strings.
                Only output if log == True.
        """
        # Reset player to fresh fight
        self.player.reset()
        self.mangle_debuff = False
        self.rip_debuff = False
        self.rake_debuff = False
        self.lacerate_debuff = False
        self.params['tigers_fury'] = False
        self.next_action = 0.0

        # Configure combat logging if requested
        self.log = log

        if self.log:
            self.player.log = True
            self.combat_log = []
        else:
            self.player.log = False

        # Same thing for swing times, except that the first swing will occur at
        # most 100 ms after the first special just to simulate some latency and
        # avoid errors from Omen procs on the first swing.
        swing_timer_start = 0.1 * np.random.rand()
        self.update_swing_times(
            swing_timer_start, self.player.swing_timer, first_swing=True
        )

        # Reset all trinkets to fresh state
        self.proc_end_times = []

        for trinket in self.trinkets:
            trinket.reset()

        # If a bear tank is providing Mangle uptime for us, then flag the
        # debuff as permanently on.
        if self.strategy['bear_mangle']:
            self.mangle_debuff = True
            self.mangle_end = np.inf

        # Pre-pop Berserk if requested
        if self.strategy['use_berserk'] and self.strategy['prepop_berserk']:
            self.apply_berserk(-1.0, prepop=True)

        # Pre-proc Clearcasting if requested
        if self.strategy['preproc_omen'] and self.player.omen:
            self.player.omen_proc = True

        # Create placeholder for time to OOM if the player goes OOM in the run
        self.time_to_oom = None

        # Create empty lists of output variables
        times = []
        damage = []
        energy = []
        combos = []

        # Run simulation
        time = 0.0
        previous_time = 0.0
        num_hot_ticks = 0

        while time <= self.fight_length:
            # Update player Mana and Energy based on elapsed simulation time
            delta_t = time - previous_time
            self.player.regen(delta_t)

            # Tabulate all damage sources in this timestep
            dmg_done = 0.0

            # Decrement cooldowns by time since last event
            self.player.gcd = max(0.0, self.player.gcd - delta_t)
            self.player.omen_icd = max(0.0, self.player.omen_icd - delta_t)
            self.player.rune_cd = max(0.0, self.player.rune_cd - delta_t)
            self.player.tf_cd = max(0.0, self.player.tf_cd - delta_t)
            self.player.berserk_cd = max(0.0, self.player.berserk_cd - delta_t)
            self.player.enrage_cd = max(0.0, self.player.enrage_cd - delta_t)
            self.player.mangle_cd = max(0.0, self.player.mangle_cd - delta_t)

            if (self.player.five_second_rule
                    and (time - self.player.last_shift >= 5)):
                self.player.five_second_rule = False

            # Check if Tiger's Fury fell off
            if self.params['tigers_fury'] and (time >= self.tf_end):
                self.drop_tigers_fury(self.tf_end)

            # Check if Berserk fell off
            if self.player.berserk and (time >= self.berserk_end):
                self.drop_berserk(self.berserk_end)

            # Check if Mangle fell off
            if self.mangle_debuff and (time >= self.mangle_end):
                self.mangle_debuff = False

                if self.log:
                    self.combat_log.append(
                        self.gen_log(self.mangle_end, 'Mangle', 'falls off')
                    )

            # Check if Savage Roar fell off
            if self.player.savage_roar and (time >= self.roar_end):
                self.player.savage_roar = False

                if log:
                    self.combat_log.append(
                        self.gen_log(self.roar_end, 'Savage Roar', 'falls off')
                    )

            # Check if a Rip tick happens at this time
            if self.rip_debuff and (time >= self.rip_ticks[0]):
                dmg_done += self.apply_bleed_damage(
                    self.rip_damage, self.rip_crit_chance, 'Rip',
                    self.rip_sr_snapshot, time
                )
                self.rip_ticks.pop(0)

            # Check if Rip fell off
            if self.rip_debuff and (time > self.rip_end - 1e-9):
                self.rip_debuff = False

                if self.log:
                    self.combat_log.append(
                        self.gen_log(self.rip_end, 'Rip', 'falls off')
                    )

            # Check if a Rake tick happens at this time
            if self.rake_debuff and (time >= self.rake_ticks[0]):
                dmg_done += self.apply_bleed_damage(
                    self.rake_damage, 0, 'Rake', self.rake_sr_snapshot, time
                )
                self.rake_ticks.pop(0)

            # Check if Rake fell off
            if self.rake_debuff and (time > self.rake_end - 1e-9):
                self.rake_debuff = False

                if self.log:
                    self.combat_log.append(
                        self.gen_log(self.rake_end, 'Rake', 'falls off')
                    )

            # Check if a Lacerate tick happens at this time
            if (self.lacerate_debuff and self.lacerate_ticks
                    and (time >= self.lacerate_ticks[0])):
                self.last_lacerate_tick = time
                dmg_done += self.apply_bleed_damage(
                    self.lacerate_damage, self.lacerate_crit_chance,
                    'Lacerate', False, time
                )
                self.lacerate_ticks.pop(0)

            # Check if Lacerate fell off
            if self.lacerate_debuff and (time > self.lacerate_end - 1e-9):
                self.lacerate_debuff = False

                if self.log:
                    self.combat_log.append(self.gen_log(
                        self.lacerate_end, 'Lacerate', 'falls off'
                    ))

            # Roll for Revitalize procs at the pre-calculated frequency
            if time >= self.revitalize_frequency * (num_hot_ticks + 1):
                num_hot_ticks += 1

                if np.random.rand() < 0.15:
                    if self.player.cat_form:
                        self.player.energy = min(100, self.player.energy + 8)
                    else:
                        self.player.rage = min(100, self.player.rage + 4)

                    if self.log:
                        self.combat_log.append(
                            self.gen_log(time, 'Revitalize', 'applied')
                        )

            # Activate or deactivate trinkets if appropriate
            for trinket in self.trinkets:
                dmg_done += trinket.update(time, self.player, self)

            # Use Enrage if appropriate
            if ((not self.player.cat_form) and (self.player.enrage_cd < 1e-9)
                    and (time < self.player.last_shift + 1.5 + 1e-9)):
                self.player.rage = min(100, self.player.rage + 20)
                self.player.enrage = True
                self.player.enrage_cd = 60.

                if self.log:
                    self.combat_log.append(
                        self.gen_log(time, 'Enrage', 'applied')
                    )

            # Check if a melee swing happens at this time
            if time == self.swing_times[0]:
                if self.player.cat_form:
                    dmg_done += self.player.swing()
                else:
                    # If we will have enough time and Energy leeway to stay in
                    # Dire Bear Form once the GCD expires, then only Maul if we
                    # will be left with enough Rage to cast Mangle or Lacerate
                    # on that global.
                    furor_cap = min(20 * self.player.furor, 85)
                    rip_refresh_pending = (
                        self.rip_debuff
                        and (self.rip_end < self.fight_length - 10)
                    )
                    energy_leeway = (
                        furor_cap - 15
                        - 10 * (self.player.gcd + self.latency)
                    )
                    shift_next = (self.player.energy > energy_leeway)

                    if rip_refresh_pending:
                        shift_next = shift_next or (
                            self.rip_end < time + self.player.gcd + 3.0
                        )

                    if self.strategy['lacerate_prio']:
                        lacerate_leeway = (
                            self.player.gcd + self.strategy['lacerate_time']
                        )
                        lacerate_next = (
                            (not self.lacerate_debuff)
                            or (self.lacerate_stacks < 5)
                            or (self.lacerate_end - time <= lacerate_leeway)
                        )
                        emergency_leeway = (
                            self.player.gcd + 3.0 + 2 * self.latency
                        )
                        emergency_lacerate_next = (
                            self.lacerate_debuff and
                            (self.lacerate_end - time <= emergency_leeway)
                        )
                        mangle_next = (not lacerate_next) and (
                            (not self.mangle_debuff)
                            or (self.mangle_end < time + self.player.gcd + 3.0)
                        )
                    else:
                        mangle_next = (self.player.mangle_cd < self.player.gcd)
                        lacerate_next = self.lacerate_debuff and (
                            (self.lacerate_stacks < 5) or
                            (self.lacerate_end < time + self.player.gcd + 4.5)
                        )
                        emergency_lacerate_next = False

                    if emergency_lacerate_next:
                        maul_rage_thresh = 23
                    elif shift_next:
                        maul_rage_thresh = 10
                    elif mangle_next:
                        maul_rage_thresh = 25
                    elif lacerate_next:
                        maul_rage_thresh = 23
                    else:
                        maul_rage_thresh = 10

                    if self.player.rage >= maul_rage_thresh:
                        dmg_done += self.player.maul(self.mangle_debuff)
                    else:
                        dmg_done += self.player.swing()

                self.swing_times.pop(0)

                if self.log:
                    self.combat_log.append(
                        ['%.3f' % time] + self.player.combat_log
                    )

                # If the swing/Maul resulted in an Omen proc, then schedule the
                # next player decision based on latency.
                if self.player.omen_proc:
                    self.next_action = time + self.latency

            # Check if we're able to act, and if so execute the optimal cast.
            self.player.combat_log = None

            if (self.player.gcd < 1e-9) and (time >= self.next_action):
                dmg_done += self.execute_rotation(time)

            # Append player's log to running combat log
            if self.log and self.player.combat_log:
                self.combat_log.append(
                    ['%.3f' % time] + self.player.combat_log
                )

            # If we entered Dire Bear Form, Tiger's Fury fell off
            if self.params['tigers_fury'] and (self.player.gcd == 1.5):
                self.drop_tigers_fury(time)

            # If a trinket proc occurred from a swing or special, apply it
            for trinket in self.trinkets:
                dmg_done += trinket.update(time, self.player, self)

            # If a proc ended at this timestep, remove it from the list
            if self.proc_end_times and (time == self.proc_end_times[0]):
                self.proc_end_times.pop(0)

            # If our Energy just dropped low enough, then cast Tiger's Fury
            #tf_energy_thresh = 30
            leeway_time = max(self.player.gcd, self.latency)
            tf_energy_thresh = 40 - 10 * (leeway_time + self.player.omen_proc)
            tf_now = (
                (self.player.energy < tf_energy_thresh)
                and (self.player.tf_cd < 1e-9) and (not self.player.berserk)
                and self.player.cat_form
            )

            if tf_now:
                # If Berserk is available, then pool to 30 Energy before
                # casting TF to maximize Berserk efficiency.
                # if self.player.berserk_cd <= leeway_time:
                #     delta_e = tf_energy_thresh - 10 - self.player.energy

                #     if delta_e < 1e-9:
                #         self.apply_tigers_fury(time)
                #     else:
                #         self.next_action = time + delta_e / 10.
                # else:
                #     self.apply_tigers_fury(time)
                self.apply_tigers_fury(time)

            # Log current parameters
            times.append(time)
            damage.append(dmg_done)
            energy.append(self.player.energy)
            combos.append(self.player.combo_points)

            # Update time
            previous_time = time
            next_swing = self.swing_times[0]
            next_action = max(time + self.player.gcd, self.next_action)
            time = min(next_action, next_swing)

            if self.rip_debuff:
                time = min(time, self.rip_ticks[0])
            if self.rake_debuff:
                time = min(time, self.rake_ticks[0])
            if self.lacerate_debuff and self.lacerate_ticks:
                time = min(time, self.lacerate_ticks[0])
            if self.proc_end_times:
                time = min(time, self.proc_end_times[0])

        # Perform a final update on trinkets at the exact fight end for
        # accurate uptime calculations. Manually deactivate any trinkets that
        # are still up, and consolidate the aura uptimes.
        aura_stats = []

        for trinket in self.trinkets:
            trinket.update(self.fight_length, self.player, self)

            try:
                if trinket.active:
                    trinket.deactivate(
                        self.player, self, time=self.fight_length
                    )

                aura_stats.append(
                    [trinket.proc_name, trinket.num_procs, trinket.uptime]
                )
            except AttributeError:
                pass

        output = (
            times, damage, energy, combos, self.player.dmg_breakdown,
            aura_stats
        )

        if self.log:
            output += (self.combat_log,)

        return output

    def iterate(self, *args):
        """Perform one iteration of a multi-replicate calculation with a
        randomized fight length.

        Returns:
            avg_dps (float): Average DPS on this iteration.
            dmg_breakdown (dict): Breakdown of cast count and damage done by
                each player ability on this iteration.
            aura_stats (list of lists): Breakdown of proc count and total
                uptime of each player cooldown on this iteration.
            time_to_oom (float): Time at which player went oom in this
                iteration. If the player did not oom, then the fight length
                used in this iteration will be returned instead.
        """
        # Since we're getting the same snapshot of the Simulation object
        # when multiple iterations are run in parallel, we need to generate a
        # new random seed.
        np.random.seed()

        # Randomize fight length to avoid haste clipping effects. We will
        # use a normal distribution centered around the target length, with
        # a standard deviation of 1 second (unhasted swing timer). Impact
        # of the choice of distribution needs to be assessed...
        base_fight_length = self.fight_length
        randomized_fight_length = base_fight_length + np.random.randn()
        self.fight_length = randomized_fight_length

        _, damage, _, _, dmg_breakdown, aura_stats = self.run()
        avg_dps = np.sum(damage) / self.fight_length
        self.fight_length = base_fight_length

        if self.time_to_oom is None:
            oom_time = randomized_fight_length
        else:
            oom_time = self.time_to_oom

        return avg_dps, dmg_breakdown, aura_stats, oom_time

    def run_replicates(self, num_replicates, detailed_output=False):
        """Perform several runs of the simulation in order to collect
        statistics on performance.

        Arguments:
            num_replicates (int): Number of replicates to run.
            detailed_output (bool): Whether to consolidate details about cast
                and mana statistics in addition to DPS values. Defaults False.

        Returns:
            dps_vals (np.ndarray): Array containing average DPS of each run.
            cast_summary (collections.OrderedDict): Dictionary containing
                averaged statistics for the number of casts and total damage
                done by each player ability over the simulated fight length.
                Output only if detailed_output == True.
            aura_summary (list of lists): Averaged statistics for the number of
                procs and total uptime of each player cooldown over the
                simulated fight length. Output only if detailed_output == True.
            oom_times (np.ndarray): Array containing times at which the player
                went oom in each run. Output only if detailed_output == True.
                If the player did not oom in a run, the corresponding entry
                will be the total fight length.
        """
        # Make sure damage and mana parameters are up to date
        self.player.calc_damage_params(**self.params)
        self.player.set_mana_regen()

        # Run replicates and consolidate results
        dps_vals = np.zeros(num_replicates)

        if detailed_output:
            oom_times = np.zeros(num_replicates)

        # Create pool of workers to run replicates in parallel
        pool = multiprocessing.Pool(processes=psutil.cpu_count(logical=False))
        i = 0

        for output in pool.imap(self.iterate, range(num_replicates)):
            avg_dps, dmg_breakdown, aura_stats, time_to_oom = output
            dps_vals[i] = avg_dps

            if not detailed_output:
                i += 1
                continue

            # Consolidate damage breakdown for the fight
            if i == 0:
                cast_sum = copy.deepcopy(dmg_breakdown)
                aura_sum = copy.deepcopy(aura_stats)
            else:
                for ability in cast_sum:
                    for key in cast_sum[ability]:
                        val = dmg_breakdown[ability][key]
                        cast_sum[ability][key] = (
                            (cast_sum[ability][key] * i + val) / (i + 1)
                        )
                for row in range(len(aura_sum)):
                    for col in [1, 2]:
                        val = aura_stats[row][col]
                        aura_sum[row][col] = (
                            (aura_sum[row][col] * i + val) / (i + 1)
                        )

            # Consolidate oom time
            oom_times[i] = time_to_oom
            i += 1

        pool.close()

        if not detailed_output:
            return dps_vals

        return dps_vals, cast_sum, aura_sum, oom_times

    def calc_deriv(self, num_replicates, param, increment, base_dps):
        """Calculate DPS increase after incrementing a player stat.

        Arguments:
            num_replicates (int): Number of replicates to run.
            param (str): Player attribute to increment.
            increment (float): Magnitude of stat increment.
            base_dps (float): Pre-calculated base DPS before stat increments.

        Returns:
            dps_delta (float): Average DPS increase after the stat increment.
                The Player attribute will be reset to its original value once
                the calculation is finished.
        """
        # Increment the stat
        original_value = getattr(self.player, param)
        setattr(self.player, param, original_value + increment)

        # For Expertise increments, implementation details demand we
        # update both 'miss_chance' and 'dodge_chance'
        if param == 'dodge_chance':
            self.player.miss_chance += increment

        # For Agility increments, also augment Attack Power and Crit
        if param == 'agility':
            self.player.attack_power += self.player.ap_mod * increment
            self.player.crit_chance += increment / 83.33 / 100.

        # Calculate DPS
        dps_vals = self.run_replicates(num_replicates)
        avg_dps = np.mean(dps_vals)

        # Reset the stat to original value
        setattr(self.player, param, original_value)

        if param == 'dodge_chance':
            self.player.miss_chance -= increment

        if param == 'agility':
            self.player.attack_power -= self.player.ap_mod * increment
            self.player.crit_chance -= increment / 83.33 / 100.

        return avg_dps - base_dps

    def calc_stat_weights(
            self, num_replicates, base_dps=None, agi_mod=1.0
    ):
        """Calculate performance derivatives for AP, hit, crit, and haste.

        Arguments:
            num_replicates (int): Number of replicates to run.
            base_dps (float): If provided, use a pre-calculated value for the
                base DPS before stat increments. Defaults to calculating base
                DPS from scratch.
            agi_mod (float): Multiplier for primary attributes to use for
                determining Agility weight. Defaults to 1.0

        Returns:
            dps_deltas (dict): Dictionary containing DPS increase from 1 AP,
                1% hit, 1% expertise, 1% crit, 1% haste, 1 Agility, 1 Armor Pen
                Rating, and 1 Weapon Damage.
            stat_weights (dict): Dictionary containing normalized stat weights
                for 1% hit, 1% expertise, 1% crit, 1% haste, 1 Agility, 1 Armor
                Pen Rating, and 1 Weapon Damage relative to 1 AP.
        """
        # First store base DPS and deltas after each stat increment
        dps_deltas = {}

        if base_dps is None:
            dps_vals = self.run_replicates(num_replicates)
            base_dps = np.mean(dps_vals)

        # For all stats, we will use a much larger increment than +1 in order
        # to see sufficient DPS increases above the simulation noise. We will
        # then linearize the increase down to a +1 increment for weight
        # calculation. This approximation is accurate as long as DPS is linear
        # in each stat up to the larger increment that was used.

        # For AP, we will use an increment of +80 AP. We also scale the
        # increase by a factor of 1.1 to account for HotW
        dps_deltas['1 AP'] = 1.0/80.0 * self.calc_deriv(
            num_replicates, 'attack_power', 80 * self.player.ap_mod, base_dps
        )

        # For hit and crit, we will use an increment of 2%.

        # For hit, we reduce miss chance by 2% if well below hit cap, and
        # increase miss chance by 2% when already capped or close.
        sign = 1 - 2 * int(
            self.player.miss_chance - self.player.dodge_chance > 0.02
        )
        dps_deltas['1% hit'] = -0.5 * sign * self.calc_deriv(
            num_replicates, 'miss_chance', sign * 0.02, base_dps
        )

        # For expertise, we mimic hit, except with dodge.
        sign = 1 - 2 * int(self.player.dodge_chance > 0.02)
        dps_deltas['1% expertise'] = -0.5 * sign * self.calc_deriv(
            num_replicates, 'dodge_chance', sign * 0.02, base_dps
        )

        # Crit is a simple increment
        dps_deltas['1% crit'] = 0.5 * self.calc_deriv(
            num_replicates, 'crit_chance', 0.02, base_dps
        )

        # For haste we will use an increment of 4%. (Note that this is 4% in
        # one slot and not four individual 1% buffs.) We implement the
        # increment by reducing the player swing timer.
        base_haste_rating = sim_utils.calc_haste_rating(
            self.player.swing_timer, multiplier=self.haste_multiplier
        )
        swing_delta = self.player.swing_timer - sim_utils.calc_swing_timer(
            base_haste_rating + 100.84, multiplier=self.haste_multiplier
        )
        dps_deltas['1% haste'] = 0.25 * self.calc_deriv(
            num_replicates, 'swing_timer', -swing_delta, base_dps
        )

        # Due to bearweaving, separate Agility weight calculation is needed
        dps_deltas['1 Agility'] = 1.0/40.0 * self.calc_deriv(
            num_replicates, 'agility', 40 * agi_mod, base_dps
        )

        # For armor pen, we use an increment of 50 Rating. Similar to hit,
        # the sign of the delta depends on if we're near the 1400 cap.
        sign = 1 - 2 * int(self.player.armor_pen_rating > 1350)
        dps_deltas['1 Armor Pen Rating'] = 1./50. * sign * self.calc_deriv(
            num_replicates, 'armor_pen_rating', sign * 50, base_dps
        )

        # For weapon damage, we use an increment of 12
        dps_deltas['1 Weapon Damage'] = 1./12. * self.calc_deriv(
            num_replicates, 'bonus_damage', 12, base_dps
        )

        # Calculate normalized stat weights
        stat_weights = {}

        for stat in dps_deltas:
            if stat != '1 AP':
                stat_weights[stat] = dps_deltas[stat] / dps_deltas['1 AP']

        return dps_deltas, stat_weights
