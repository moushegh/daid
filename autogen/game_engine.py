"""
D&D AI Game Engine
------------------
Pure-Python deterministic game mechanics: dice, characters, combat, state.
No LLM calls — this module handles all rules and randomness.
"""

import random
import re
import math
import json
from dataclasses import dataclass, field, asdict
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Dice System
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class DiceResult:
    notation: str
    rolls: list[int]
    modifier: int
    total: int
    is_critical: bool = False   # nat 20 on d20
    is_fumble: bool = False     # nat 1 on d20

    def __str__(self):
        parts = [f"🎲 {self.notation}: "]
        if len(self.rolls) == 1:
            parts.append(str(self.rolls[0]))
        else:
            parts.append(f"({' + '.join(str(r) for r in self.rolls)})")
        if self.modifier > 0:
            parts.append(f" + {self.modifier}")
        elif self.modifier < 0:
            parts.append(f" - {abs(self.modifier)}")
        parts.append(f" = **{self.total}**")
        if self.is_critical:
            parts.append(" ⚡ CRITICAL!")
        elif self.is_fumble:
            parts.append(" 💀 FUMBLE!")
        return "".join(parts)


def roll_dice(notation: str) -> DiceResult:
    """
    Roll dice from notation like '2d6+3', '1d20', '1d8-1'.
    Returns a DiceResult with all details logged.
    """
    notation = notation.strip().lower()
    match = re.match(r'^(\d+)d(\d+)([+-]\d+)?$', notation)
    if not match:
        raise ValueError(f"Invalid dice notation: {notation}")

    count = int(match.group(1))
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0

    if sides not in (4, 6, 8, 10, 12, 20, 100):
        raise ValueError(f"Invalid die type: d{sides}")
    if count < 1 or count > 20:
        raise ValueError(f"Invalid dice count: {count}")

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls) + modifier

    is_critical = (sides == 20 and count == 1 and rolls[0] == 20)
    is_fumble = (sides == 20 and count == 1 and rolls[0] == 1)

    return DiceResult(
        notation=notation,
        rolls=rolls,
        modifier=modifier,
        total=total,
        is_critical=is_critical,
        is_fumble=is_fumble,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Character System
# ──────────────────────────────────────────────────────────────────────────────

def modifier(score: int) -> int:
    """D&D attribute modifier: floor((score - 10) / 2)"""
    return math.floor((score - 10) / 2)


@dataclass
class Spell:
    name: str
    level: int              # 0 = cantrip
    damage_dice: str        # e.g. '2d6' or '' for healing/utility
    heal_dice: str          # e.g. '1d8+3' for healing spells
    spell_type: str         # 'attack', 'heal', 'utility', 'save'
    save_ability: str       # e.g. 'dexterity' for save-type spells
    save_dc: int            # DC for the saving throw
    range: str              # 'touch', 'melee', '60ft', '120ft'
    description: str
    uses_remaining: int     # spell slots; cantrips = 99


@dataclass
class Weapon:
    name: str
    damage_dice: str        # e.g. '1d8'
    attack_modifier: int    # proficiency + ability mod
    ability: str            # 'strength' or 'dexterity'
    weapon_range: str       # 'melee' or '30ft' etc.
    description: str = ""


@dataclass
class Character:
    name: str
    char_class: str         # Fighter, Wizard, Rogue, Cleric
    race: str
    level: int = 1

    # Core attributes (3–18)
    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10

    # Derived stats
    max_hp: int = 10
    current_hp: int = 10
    armor_class: int = 10
    proficiency_bonus: int = 2

    # Equipment & abilities
    weapons: list = field(default_factory=list)
    spells: list = field(default_factory=list)
    abilities: list = field(default_factory=list)
    inventory: list = field(default_factory=list)

    # State
    alive: bool = True
    incapacitated: bool = False
    conditions: list = field(default_factory=list)  # 'poisoned', 'blessed', etc.

    # Is this a player character or NPC/monster?
    is_player: bool = True
    is_monster: bool = False

    def get_modifier(self, ability: str) -> int:
        ability = ability.lower()
        scores = {
            'strength': self.strength,
            'dexterity': self.dexterity,
            'constitution': self.constitution,
            'intelligence': self.intelligence,
            'wisdom': self.wisdom,
            'charisma': self.charisma,
        }
        return modifier(scores.get(ability, 10))

    def get_attack_modifier(self, weapon: Optional[Weapon] = None) -> int:
        if weapon:
            return self.get_modifier(weapon.ability) + self.proficiency_bonus
        # Default: use STR for melee
        return self.get_modifier('strength') + self.proficiency_bonus

    def get_spell_attack_modifier(self) -> int:
        if self.char_class == 'Wizard':
            return self.get_modifier('intelligence') + self.proficiency_bonus
        elif self.char_class == 'Cleric':
            return self.get_modifier('wisdom') + self.proficiency_bonus
        return self.proficiency_bonus

    def get_spell_save_dc(self) -> int:
        return 8 + self.get_spell_attack_modifier()

    def take_damage(self, amount: int) -> dict:
        """Apply damage. Returns dict with details."""
        actual = max(0, amount)
        self.current_hp -= actual
        result = {
            'damage': actual,
            'remaining_hp': self.current_hp,
            'incapacitated': False,
        }
        if self.current_hp <= 0:
            self.current_hp = 0
            self.alive = False
            self.incapacitated = True
            result['incapacitated'] = True
        return result

    def heal(self, amount: int) -> dict:
        """Heal the character. Returns dict with details."""
        if not self.alive and self.current_hp <= 0:
            # Revive with healing
            self.alive = True
            self.incapacitated = False
        old_hp = self.current_hp
        self.current_hp = min(self.max_hp, self.current_hp + amount)
        actual = self.current_hp - old_hp
        return {
            'healed': actual,
            'remaining_hp': self.current_hp,
            'max_hp': self.max_hp,
        }

    def short_status(self) -> str:
        status = "DEAD" if not self.alive else "OK"
        conditions = f" [{', '.join(self.conditions)}]" if self.conditions else ""
        return f"{self.name} ({self.char_class}): HP {self.current_hp}/{self.max_hp} [{status}]{conditions}"

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'char_class': self.char_class,
            'race': self.race,
            'level': self.level,
            'strength': self.strength,
            'dexterity': self.dexterity,
            'constitution': self.constitution,
            'intelligence': self.intelligence,
            'wisdom': self.wisdom,
            'charisma': self.charisma,
            'max_hp': self.max_hp,
            'current_hp': self.current_hp,
            'armor_class': self.armor_class,
            'alive': self.alive,
            'incapacitated': self.incapacitated,
            'conditions': self.conditions,
            'is_player': self.is_player,
            'weapons': [w.name for w in self.weapons] if self.weapons else [],
            'spells': [s.name for s in self.spells] if self.spells else [],
            'abilities': self.abilities,
            'inventory': self.inventory,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Combat Engine
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AttackResult:
    attacker: str
    target: str
    attack_roll: DiceResult
    hit: bool
    damage: int = 0
    damage_roll: Optional[DiceResult] = None
    target_hp: int = 0
    target_incapacitated: bool = False
    critical: bool = False
    fumble: bool = False
    description: str = ""

    def __str__(self):
        lines = [f"⚔️ {self.attacker} attacks {self.target}!"]
        lines.append(f"  Attack: {self.attack_roll}")
        if self.fumble:
            lines.append(f"  💀 FUMBLE! The attack misses wildly!")
        elif not self.hit:
            lines.append(f"  ❌ MISS! (needed AC {self.target_hp})")
        else:
            if self.critical:
                lines.append(f"  ⚡ CRITICAL HIT!")
            lines.append(f"  ✅ HIT! Damage: {self.damage_roll} → {self.damage} damage")
            lines.append(f"  {self.target}: {self.target_hp} HP remaining")
            if self.target_incapacitated:
                lines.append(f"  💀 {self.target} falls!")
        return "\n".join(lines)


class CombatEngine:

    @staticmethod
    def roll_initiative(characters: list[Character]) -> list[tuple[Character, DiceResult]]:
        """Roll initiative for all characters. Returns sorted list of (character, roll)."""
        results = []
        for char in characters:
            if char.alive and not char.incapacitated:
                roll = roll_dice("1d20")
                init_total = roll.total + char.get_modifier('dexterity')
                results.append((char, roll, init_total))
        results.sort(key=lambda x: x[2], reverse=True)
        return [(char, roll) for char, roll, _ in results]

    @staticmethod
    def attack(attacker: Character, target: Character,
               weapon: Optional[Weapon] = None) -> AttackResult:
        """Resolve a melee or ranged attack."""
        if not attacker.alive or attacker.incapacitated:
            return AttackResult(
                attacker=attacker.name, target=target.name,
                attack_roll=DiceResult("1d20", [0], 0, 0),
                hit=False, description=f"{attacker.name} is incapacitated!"
            )

        # Attack roll
        atk_roll = roll_dice("1d20")
        atk_mod = attacker.get_attack_modifier(weapon)
        atk_total = atk_roll.total + atk_mod

        result = AttackResult(
            attacker=attacker.name,
            target=target.name,
            attack_roll=atk_roll,
            hit=False,
            critical=atk_roll.is_critical,
            fumble=atk_roll.is_fumble,
        )

        # Natural 1 = auto miss
        if atk_roll.is_fumble:
            result.description = f"{attacker.name} fumbles! The attack goes wide."
            return result

        # Natural 20 = auto hit + double damage dice
        # Normal hit check
        if atk_roll.is_critical or atk_total >= target.armor_class:
            result.hit = True

            # Damage roll
            if weapon:
                dmg_dice = weapon.damage_dice
            else:
                dmg_dice = "1d4"  # unarmed

            dmg_roll = roll_dice(dmg_dice)
            damage = dmg_roll.total

            if atk_roll.is_critical:
                # Double damage on crit
                extra = roll_dice(dmg_dice)
                damage += extra.total
                result.critical = True

            # Add ability modifier to damage
            if weapon:
                damage += attacker.get_modifier(weapon.ability)
            else:
                damage += attacker.get_modifier('strength')

            damage = max(1, damage)  # Minimum 1 damage on hit

            dmg_result = target.take_damage(damage)
            result.damage = damage
            result.damage_roll = dmg_roll
            result.target_hp = target.current_hp
            result.target_incapacitated = dmg_result['incapacitated']

            if result.target_incapacitated:
                result.description = (
                    f"{attacker.name} strikes {target.name} for {damage} damage! "
                    f"{target.name} falls to the ground!"
                )
            else:
                result.description = (
                    f"{attacker.name} hits {target.name} for {damage} damage! "
                    f"({target.current_hp}/{target.max_hp} HP remaining)"
                )
        else:
            result.target_hp = target.armor_class  # for display
            result.description = (
                f"{attacker.name} swings at {target.name} but misses! "
                f"(rolled {atk_total} vs AC {target.armor_class})"
            )

        return result

    @staticmethod
    def spell_attack(caster: Character, spell: Spell,
                     target: Character) -> dict:
        """Resolve a spell attack or save-based spell."""
        if not caster.alive or caster.incapacitated:
            return {
                'success': False,
                'description': f"{caster.name} is incapacitated and cannot cast!",
                'damage': 0,
                'healed': 0,
            }

        # Check spell uses
        if spell.uses_remaining <= 0:
            return {
                'success': False,
                'description': f"{caster.name} has no more uses of {spell.name}!",
                'damage': 0,
                'healed': 0,
            }

        spell.uses_remaining -= 1
        result = {
            'spell_name': spell.name,
            'caster': caster.name,
            'target': target.name,
            'success': False,
            'damage': 0,
            'healed': 0,
            'rolls': [],
            'description': '',
        }

        if spell.spell_type == 'heal':
            if spell.heal_dice:
                heal_roll = roll_dice(spell.heal_dice)
                heal_amount = max(1, heal_roll.total)
                result['rolls'].append(str(heal_roll))
            else:
                heal_amount = 4 + caster.get_modifier('wisdom')

            heal_result = target.heal(heal_amount)
            result['success'] = True
            result['healed'] = heal_result['healed']
            result['description'] = (
                f"✨ {caster.name} casts {spell.name} on {target.name}! "
                f"Healed for {heal_result['healed']} HP "
                f"({target.current_hp}/{target.max_hp} HP)"
            )
            return result

        if spell.spell_type == 'attack':
            # Spell attack roll
            atk_roll = roll_dice("1d20")
            atk_mod = caster.get_spell_attack_modifier()
            atk_total = atk_roll.total + atk_mod
            result['rolls'].append(str(atk_roll))

            if atk_roll.is_fumble:
                result['description'] = (
                    f"🔮 {caster.name} casts {spell.name} at {target.name}... "
                    f"but the spell fizzles! (Natural 1)"
                )
                return result

            if atk_roll.is_critical or atk_total >= target.armor_class:
                result['success'] = True
                if spell.damage_dice:
                    dmg_roll = roll_dice(spell.damage_dice)
                    damage = dmg_roll.total
                    if atk_roll.is_critical:
                        extra = roll_dice(spell.damage_dice)
                        damage += extra.total
                    result['rolls'].append(str(dmg_roll))
                else:
                    damage = 4

                damage = max(1, damage)
                dmg_result = target.take_damage(damage)
                result['damage'] = damage
                result['description'] = (
                    f"🔮 {caster.name} casts {spell.name} at {target.name}! "
                    f"Hit for {damage} damage! "
                    f"({target.current_hp}/{target.max_hp} HP)"
                )
                if dmg_result['incapacitated']:
                    result['description'] += f" 💀 {target.name} is destroyed!"
            else:
                result['description'] = (
                    f"🔮 {caster.name} casts {spell.name} at {target.name}... "
                    f"but it misses! (rolled {atk_total} vs AC {target.armor_class})"
                )
            return result

        if spell.spell_type == 'save':
            # Target makes a saving throw
            save_roll = roll_dice("1d20")
            save_mod = target.get_modifier(spell.save_ability)
            save_total = save_roll.total + save_mod
            result['rolls'].append(str(save_roll))

            if save_total >= spell.save_dc:
                # Saved — half damage or no effect
                if spell.damage_dice:
                    dmg_roll = roll_dice(spell.damage_dice)
                    damage = max(1, dmg_roll.total // 2)
                    target.take_damage(damage)
                    result['damage'] = damage
                    result['description'] = (
                        f"🔮 {caster.name} casts {spell.name}! "
                        f"{target.name} saves (rolled {save_total} vs DC {spell.save_dc}) "
                        f"— half damage: {damage}! ({target.current_hp}/{target.max_hp} HP)"
                    )
                else:
                    result['description'] = (
                        f"🔮 {caster.name} casts {spell.name}! "
                        f"{target.name} resists! (rolled {save_total} vs DC {spell.save_dc})"
                    )
            else:
                result['success'] = True
                if spell.damage_dice:
                    dmg_roll = roll_dice(spell.damage_dice)
                    damage = max(1, dmg_roll.total)
                    dmg_result = target.take_damage(damage)
                    result['damage'] = damage
                    result['description'] = (
                        f"🔮 {caster.name} casts {spell.name}! "
                        f"{target.name} fails the save (rolled {save_total} vs DC {spell.save_dc}) "
                        f"— {damage} damage! ({target.current_hp}/{target.max_hp} HP)"
                    )
                    if dmg_result['incapacitated']:
                        result['description'] += f" 💀 {target.name} is destroyed!"
                else:
                    result['description'] = (
                        f"🔮 {caster.name} casts {spell.name} on {target.name}! "
                        f"The spell takes effect!"
                    )
            return result

        # Utility spells
        result['success'] = True
        result['description'] = f"🔮 {caster.name} casts {spell.name}. {spell.description}"
        return result


# ──────────────────────────────────────────────────────────────────────────────
# Ability Checks
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    character: str
    ability: str
    dc: int
    roll: DiceResult
    modifier: int
    total: int
    success: bool
    critical: bool = False
    fumble: bool = False
    description: str = ""

    def __str__(self):
        status = "✅ SUCCESS" if self.success else "❌ FAILURE"
        return (
            f"🎯 {self.character} — {self.ability.title()} check (DC {self.dc}): "
            f"{self.roll} + {self.modifier} = {self.total} → {status}"
        )


def ability_check(character: Character, ability: str, dc: int,
                  skill_bonus: int = 0) -> CheckResult:
    """
    Perform an ability check: 1d20 + modifier + skill_bonus >= DC
    """
    roll = roll_dice("1d20")
    mod = character.get_modifier(ability)
    total = roll.total + mod + skill_bonus

    # Natural 20/1 rules
    if roll.is_critical:
        success = True
    elif roll.is_fumble:
        success = False
    else:
        success = total >= dc

    return CheckResult(
        character=character.name,
        ability=ability,
        dc=dc,
        roll=roll,
        modifier=mod + skill_bonus,
        total=total,
        success=success,
        critical=roll.is_critical,
        fumble=roll.is_fumble,
        description=(
            f"{character.name} {'succeeds' if success else 'fails'} "
            f"the {ability.title()} check (DC {dc}): "
            f"rolled {roll.total} + {mod + skill_bonus} = {total}"
        ),
    )


def saving_throw(character: Character, ability: str, dc: int) -> CheckResult:
    """Saving throw — same as ability check but labeled differently."""
    result = ability_check(character, ability, dc)
    result.description = result.description.replace("check", "saving throw")
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Game State
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class GameState:
    scene_index: int = 0
    scene_title: str = ""
    round_number: int = 0
    max_rounds: int = 100
    turn_in_round: int = 0

    party: list[Character] = field(default_factory=list)
    enemies: list[Character] = field(default_factory=list)
    npcs: list[Character] = field(default_factory=list)

    in_combat: bool = False
    combat_order: list[str] = field(default_factory=list)  # character names
    combat_round: int = 0

    game_over: bool = False
    victory: bool = False
    game_over_reason: str = ""

    narrative_log: list[str] = field(default_factory=list)
    dice_log: list[str] = field(default_factory=list)

    # Special flags
    has_amulet: bool = False        # party found the blessed amulet
    read_runes: bool = False         # party read the ancient runes
    boss_weakened: bool = False      # amulet bonus active

    def get_character(self, name: str) -> Optional[Character]:
        """Find a character by name (party, enemies, or NPCs)."""
        for c in self.party + self.enemies + self.npcs:
            if c.name.lower() == name.lower():
                return c
        return None

    def get_alive_party(self) -> list[Character]:
        return [c for c in self.party if c.alive]

    def get_alive_enemies(self) -> list[Character]:
        return [c for c in self.enemies if c.alive]

    def check_tpk(self) -> bool:
        """Total party kill check."""
        return len(self.get_alive_party()) == 0

    def check_all_enemies_dead(self) -> bool:
        return len(self.enemies) > 0 and len(self.get_alive_enemies()) == 0

    def check_timeout(self) -> bool:
        return self.round_number >= self.max_rounds

    def party_status(self) -> str:
        lines = ["═══ PARTY STATUS ═══"]
        for c in self.party:
            hp_bar_len = 10
            hp_pct = c.current_hp / c.max_hp if c.max_hp > 0 else 0
            filled = int(hp_pct * hp_bar_len)
            bar = "█" * filled + "░" * (hp_bar_len - filled)
            status = "💀 DEAD" if not c.alive else f"{bar} {c.current_hp}/{c.max_hp}"
            lines.append(f"  {c.name} ({c.char_class}): {status}")
        return "\n".join(lines)

    def enemy_status(self) -> str:
        if not self.enemies:
            return "No enemies present."
        lines = ["═══ ENEMIES ═══"]
        for e in self.enemies:
            if e.alive:
                lines.append(f"  {e.name}: HP {e.current_hp}/{e.max_hp}")
            else:
                lines.append(f"  {e.name}: 💀 DEFEATED")
        return "\n".join(lines)

    def summary(self) -> str:
        parts = [
            f"Scene {self.scene_index + 1}: {self.scene_title}",
            f"Round: {self.round_number}",
        ]
        if self.in_combat:
            parts.append(f"Combat Round: {self.combat_round}")
            parts.append(f"Initiative: {', '.join(self.combat_order)}")
        parts.append(self.party_status())
        if self.enemies:
            parts.append(self.enemy_status())
        if self.has_amulet:
            parts.append("🔮 Party has the Blessed Amulet")
        if self.read_runes:
            parts.append("📜 Party read the Ancient Runes")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return {
            'scene_index': self.scene_index,
            'scene_title': self.scene_title,
            'round_number': self.round_number,
            'max_rounds': self.max_rounds,
            'in_combat': self.in_combat,
            'combat_round': self.combat_round,
            'combat_order': self.combat_order,
            'game_over': self.game_over,
            'victory': self.victory,
            'game_over_reason': self.game_over_reason,
            'has_amulet': self.has_amulet,
            'read_runes': self.read_runes,
            'party': [c.to_dict() for c in self.party],
            'enemies': [c.to_dict() for c in self.enemies],
        }
