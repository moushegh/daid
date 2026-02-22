"""
D&D AI Adventure Module
------------------------
Pre-built adventure: "The Crypt of the Shadow Lord"
Defines scenes, NPCs, encounters, and win/loss conditions.
"""

from game_engine import (
    Character, Weapon, Spell, GameState, CombatEngine,
    roll_dice, ability_check, saving_throw, modifier,
)


# ──────────────────────────────────────────────────────────────────────────────
# Party Creation — Pre-built characters (deterministic demo)
# ──────────────────────────────────────────────────────────────────────────────

def create_party() -> list[Character]:
    """Create the 4-member adventuring party."""

    thorin = Character(
        name="Thorin",
        char_class="Fighter",
        race="Dwarf",
        level=3,
        strength=16, dexterity=12, constitution=14,
        intelligence=10, wisdom=11, charisma=13,
        max_hp=28, current_hp=28,
        armor_class=16,  # Chain mail + shield
        proficiency_bonus=2,
        weapons=[
            Weapon("Battleaxe", "1d8", 5, "strength", "melee",
                   "A sturdy dwarven battleaxe"),
        ],
        abilities=["Second Wind (heal 1d10+3, once per rest)",
                    "Action Surge (extra action, once per rest)"],
        inventory=["Chain mail", "Shield", "Explorer's pack", "50ft rope"],
    )

    elara = Character(
        name="Elara",
        char_class="Wizard",
        race="Elf",
        level=3,
        strength=8, dexterity=14, constitution=12,
        intelligence=17, wisdom=13, charisma=11,
        max_hp=18, current_hp=18,
        armor_class=12,  # Mage armor (base 10 + DEX)
        proficiency_bonus=2,
        weapons=[
            Weapon("Quarterstaff", "1d6", 0, "strength", "melee",
                   "A simple wooden staff"),
        ],
        spells=[
            Spell("Fire Bolt", 0, "1d10", "", "attack", "", 0,
                  "120ft", "A mote of fire streaks toward a target", 99),
            Spell("Magic Missile", 1, "3d4+3", "", "attack", "", 0,
                  "120ft", "Three glowing darts of force unerringly strike", 3),
            Spell("Burning Hands", 1, "3d6", "", "save", "dexterity", 13,
                  "15ft cone", "Flames shoot from fingertips in a cone", 2),
            Spell("Shield", 1, "", "", "utility", "", 0,
                  "self", "+5 AC until next turn as a reaction", 2),
        ],
        abilities=["Arcane Recovery (recover 1 spell slot on short rest)"],
        inventory=["Spellbook", "Component pouch", "Scholar's pack"],
    )

    shadow = Character(
        name="Shadow",
        char_class="Rogue",
        race="Halfling",
        level=3,
        strength=10, dexterity=17, constitution=12,
        intelligence=14, wisdom=12, charisma=14,
        max_hp=21, current_hp=21,
        armor_class=14,  # Leather armor + DEX
        proficiency_bonus=2,
        weapons=[
            Weapon("Shortsword", "1d6", 5, "dexterity", "melee",
                   "A keen-edged shortsword"),
            Weapon("Shortbow", "1d6", 5, "dexterity", "80ft",
                   "A compact shortbow"),
        ],
        spells=[],
        abilities=["Sneak Attack (+2d6 damage when advantage or ally adjacent)",
                    "Cunning Action (Dash, Disengage, or Hide as bonus action)",
                    "Thieves' Tools proficiency"],
        inventory=["Leather armor", "Thieves' tools", "Burglar's pack", "Daggers x3"],
    )

    aldric = Character(
        name="Aldric",
        char_class="Cleric",
        race="Human",
        level=3,
        strength=14, dexterity=10, constitution=14,
        intelligence=12, wisdom=16, charisma=13,
        max_hp=24, current_hp=24,
        armor_class=18,  # Chain mail + shield + cleric bonus
        proficiency_bonus=2,
        weapons=[
            Weapon("Mace", "1d6", 4, "strength", "melee",
                   "A sturdy iron mace blessed by the temple"),
        ],
        spells=[
            Spell("Sacred Flame", 0, "1d8", "", "save", "dexterity", 13,
                  "60ft", "Radiant flame descends on a target", 99),
            Spell("Cure Wounds", 1, "", "1d8+3", "heal", "", 0,
                  "touch", "Healing energy flows into the target", 4),
            Spell("Guiding Bolt", 1, "4d6", "", "attack", "", 0,
                  "120ft", "A bolt of radiant light streaks toward a target", 2),
            Spell("Turn Undead", 1, "", "", "save", "wisdom", 13,
                  "30ft", "Undead must save or flee for 1 minute", 2),
            Spell("Healing Word", 1, "", "1d4+3", "heal", "", 0,
                  "60ft", "Quick healing word spoken to an ally", 3),
        ],
        abilities=["Channel Divinity: Turn Undead",
                    "Preserve Life (distribute healing among allies)"],
        inventory=["Chain mail", "Shield", "Holy symbol", "Priest's pack"],
    )

    return [thorin, elara, shadow, aldric]


# ──────────────────────────────────────────────────────────────────────────────
# Monster/NPC Definitions
# ──────────────────────────────────────────────────────────────────────────────

def create_skeleton() -> Character:
    return Character(
        name="Skeleton",
        char_class="Undead",
        race="Undead",
        strength=10, dexterity=14, constitution=15,
        intelligence=6, wisdom=8, charisma=5,
        max_hp=13, current_hp=13,
        armor_class=13,
        proficiency_bonus=2,
        weapons=[
            Weapon("Rusty Sword", "1d6", 4, "dexterity", "melee",
                   "A corroded but sharp blade"),
        ],
        is_player=False, is_monster=True,
    )


def create_shadow_lord() -> Character:
    return Character(
        name="Shadow Lord",
        char_class="Undead Boss",
        race="Undead",
        strength=16, dexterity=14, constitution=16,
        intelligence=14, wisdom=12, charisma=16,
        max_hp=55, current_hp=55,
        armor_class=15,
        proficiency_bonus=3,
        weapons=[
            Weapon("Shadow Blade", "1d8", 6, "strength", "melee",
                   "A blade of pure darkness"),
        ],
        spells=[
            Spell("Shadow Bolt", 0, "2d8", "", "attack", "", 0,
                  "60ft", "A bolt of necrotic energy", 99),
            Spell("Life Drain", 1, "2d6", "", "attack", "", 0,
                  "touch", "Drains life force from the target", 3),
            Spell("Dark Aura", 1, "1d6", "", "save", "constitution", 14,
                  "20ft", "All living creatures in range take necrotic damage", 2),
        ],
        abilities=["Shadow Step (teleport 30ft as bonus action)",
                    "Summon Undead (at half HP, summon 2 skeletons)"],
        is_player=False, is_monster=True,
    )


def create_village_elder() -> Character:
    """Non-combatant NPC."""
    return Character(
        name="Elder Maren",
        char_class="Commoner",
        race="Human",
        strength=8, dexterity=10, constitution=10,
        intelligence=14, wisdom=16, charisma=13,
        max_hp=4, current_hp=4,
        armor_class=10,
        is_player=False, is_monster=False,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Scene Definitions
# ──────────────────────────────────────────────────────────────────────────────

SCENES = [
    {
        "index": 0,
        "title": "The Village of Millhaven",
        "type": "roleplay",
        "description": (
            "The small village of Millhaven huddles beneath a grey sky. Shuttered windows and "
            "barred doors line the muddy main road. At the village square, an elderly woman — "
            "Elder Maren — waits with haunted eyes. Behind her, fresh graves dot the churchyard. "
            "For three nights, the dead have risen from the ancient crypt on the hill, attacking "
            "anyone caught outside after dark. The village is desperate."
        ),
        "dm_instructions": (
            "Narrate the village scene and Elder Maren's plea. She offers the party 200 gold "
            "and a Blessed Amulet (an ancient relic that weakens undead). Each player should "
            "have a chance to ask questions or roleplay. After 2-3 exchanges, guide the party "
            "toward departing for the crypt. Key information Elder Maren can share:\n"
            "- The crypt is a 30-minute walk east, on Gravestone Hill\n"
            "- The undead rise at midnight and return before dawn\n"
            "- An ancient evil called the Shadow Lord was sealed there centuries ago\n"
            "- The Blessed Amulet was passed down by the village founders\n"
            "End this scene by having the party depart for the crypt."
        ),
        "checks": [
            {"type": "charisma", "dc": 10, "character": "any",
             "success": "Elder Maren also reveals a secret back entrance to the crypt.",
             "failure": "Elder Maren has nothing more to share."},
        ],
        "enemies": [],
        "max_rounds": 8,
        "rewards": ["Blessed Amulet", "200 gold (promised on return)"],
    },
    {
        "index": 1,
        "title": "The Crypt Entrance",
        "type": "exploration_combat",
        "description": (
            "A crumbling stone stairway descends into darkness beneath the hillside. "
            "Ancient runes are carved into the archway above the entrance. The air grows cold "
            "and carries the smell of decay. Cobwebs and dust cover the narrow corridor. "
            "The passage opens into a chamber with three stone coffins — their lids pushed aside. "
            "The bones within stir as torchlight reaches them."
        ),
        "dm_instructions": (
            "Guide the party through the crypt entrance. Present these challenges in order:\n"
            "1. RUNES: Allow an Intelligence check (DC 13) to read the ancient runes. "
            "Success reveals the Shadow Lord's weakness to radiant damage and holy magic.\n"
            "2. TRAP: The corridor has a pressure plate. Perception check (DC 14) to spot it. "
            "If failed, the triggering character takes 2d6 piercing damage from a dart trap.\n"
            "3. COMBAT: 3 Skeletons rise from the coffins and attack. Run combat with initiative. "
            "After the skeletons are defeated, the party can proceed to the inner chamber.\n"
            "Narrate the environment vividly. Keep it atmospheric and tense."
        ),
        "checks": [
            {"type": "intelligence", "dc": 13, "character": "Elara",
             "purpose": "reading_runes",
             "success": "The runes reveal the Shadow Lord is vulnerable to radiant damage and holy relics.",
             "failure": "The runes are too weathered to decipher."},
            {"type": "perception", "dc": 14, "character": "Shadow",
             "purpose": "spot_trap",
             "success": "Shadow spots the pressure plate just in time!",
             "failure": "A dart trap fires!"},
        ],
        "enemies": ["skeleton", "skeleton", "skeleton"],
        "max_rounds": 15,
        "rewards": ["Ancient knowledge (if runes read)"],
    },
    {
        "index": 2,
        "title": "The Shadow Lord's Chamber",
        "type": "boss_combat",
        "description": (
            "The corridor opens into a vast underground chamber. Black stone pillars line the walls, "
            "carved with writhing figures of the damned. At the far end, a dark altar pulses with "
            "purple-black energy. Above it, a figure of pure shadow takes form — the Shadow Lord. "
            "His eyes burn with cold violet fire as he speaks in a voice like grinding stone: "
            "'Foolish mortals. You dare enter my domain? Your souls will join my army.'\n"
            "The temperature drops. The shadows themselves seem to reach for the party."
        ),
        "dm_instructions": (
            "This is the final boss battle. The Shadow Lord attacks immediately.\n"
            "COMBAT RULES:\n"
            "- Shadow Lord fights intelligently: targets the wizard or cleric first\n"
            "- Uses Shadow Bolt at range, Shadow Blade in melee\n"
            "- When HP drops below half (27), he summons 2 Skeleton minions and uses Dark Aura\n"
            "- If the party has the Blessed Amulet, all attacks against him get +2 to hit\n"
            "- If the party read the runes, radiant spells (Sacred Flame, Guiding Bolt) deal double damage\n\n"
            "VICTORY: Shadow Lord reaches 0 HP. Narrate his destruction spectacularly.\n"
            "DEFEAT: All party members reach 0 HP. Narrate the party's fall.\n"
            "Run full combat with initiative order. Each round: all combatants act in order.\n"
            "Keep narration dramatic and vivid."
        ),
        "enemies": ["shadow_lord"],
        "max_rounds": 30,
        "rewards": ["Shadow Lord's defeat", "Village saved"],
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Adventure State Initialization
# ──────────────────────────────────────────────────────────────────────────────

def create_game_state() -> GameState:
    """Create the initial game state for the adventure."""
    party = create_party()
    state = GameState(
        scene_index=0,
        scene_title=SCENES[0]["title"],
        party=party,
        max_rounds=100,
    )
    return state


def get_current_scene(state: GameState) -> dict:
    """Get the current scene definition."""
    if state.scene_index < len(SCENES):
        return SCENES[state.scene_index]
    return SCENES[-1]


def spawn_enemies(state: GameState, scene: dict) -> list[Character]:
    """Spawn enemies for the current scene."""
    enemies = []
    enemy_counts = {}
    for enemy_type in scene.get("enemies", []):
        if enemy_type == "skeleton":
            skel = create_skeleton()
            enemy_counts["Skeleton"] = enemy_counts.get("Skeleton", 0) + 1
            count = enemy_counts["Skeleton"]
            if count > 1:
                skel.name = f"Skeleton {count}"
            enemies.append(skel)
        elif enemy_type == "shadow_lord":
            enemies.append(create_shadow_lord())
    state.enemies = enemies
    return enemies


def advance_scene(state: GameState) -> bool:
    """Advance to the next scene. Returns False if adventure is complete."""
    state.scene_index += 1
    if state.scene_index >= len(SCENES):
        return False
    scene = SCENES[state.scene_index]
    state.scene_title = scene["title"]
    state.in_combat = False
    state.combat_order = []
    state.combat_round = 0
    state.enemies = []
    return True


def check_game_over(state: GameState) -> tuple[bool, bool, str]:
    """
    Check if the game is over.
    Returns (is_over, is_victory, reason).
    """
    # TPK = defeat
    if state.check_tpk():
        return True, False, "Total Party Kill — all adventurers have fallen!"

    # Timeout = defeat
    if state.check_timeout():
        return True, False, "The crypt collapses! The party ran out of time."

    # Boss dead in final scene = victory
    if state.scene_index == 2 and state.check_all_enemies_dead():
        return True, True, "The Shadow Lord is destroyed! The village of Millhaven is saved!"

    return False, False, ""


# ──────────────────────────────────────────────────────────────────────────────
# Scene Text Helpers
# ──────────────────────────────────────────────────────────────────────────────

VICTORY_EPILOGUE = """
🏆 VICTORY! 🏆

As the Shadow Lord crumbles to dust, the dark energy dissipates. The violet fire in his 
eyes flickers and dies. The crypt falls silent — truly silent — for the first time in centuries.

The party emerges from the crypt into warm sunlight. The grey clouds have parted, and birdsong
fills the air. When they return to Millhaven, the entire village turns out to cheer.

Elder Maren weeps with joy. "You have saved us all. The dead will rest again."

The heroes of Millhaven — their names will be remembered for generations.

════════════════════════════════════════
  ADVENTURE COMPLETE — VICTORY
════════════════════════════════════════
"""

DEFEAT_EPILOGUE = """
💀 DEFEAT 💀

The last hero falls. The Shadow Lord's laughter echoes through the crypt as darkness claims
the adventurers. Their sacrifice was in vain.

Without anyone to stop him, the Shadow Lord's power grows unchecked. Night after night, his 
undead army swells. Millhaven is abandoned. Then the next village. And the next.

The shadow spreads across the land, and songs are sung of the brave souls who tried — and failed.

════════════════════════════════════════
  ADVENTURE COMPLETE — DEFEAT
════════════════════════════════════════
"""
