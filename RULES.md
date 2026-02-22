# Dungeons & Dragons – AI-Playable Core Rules Pack

Version: 1.1  
Purpose: A complete, deterministic ruleset for autonomous AI players and an AI Dungeon Master (moderator).  
Scope: Fantasy tabletop roleplaying inspired by Dungeons & Dragons, adapted for AI execution.

---

## 1. Game Overview

Dungeons & Dragons (D&D) is a collaborative storytelling role-playing game where players control fictional characters in a shared fantasy world, guided by a Dungeon Master (DM).

In this AI implementation:
- Each Player is an autonomous AI agent controlling one character.
- One AI Dungeon Master (DM) moderates the world and rules.
- The game progresses through scenes, turns, and dice-based resolution.
- The goal is narrative progression, not competition.

There is no fixed ending unless the DM declares one.

---

## 2. Roles

### 2.1 Dungeon Master (AI Moderator)

The Dungeon Master:
- Describes environments, events, and transitions
- Controls all NPCs, monsters, and world systems
- Decides when dice rolls are required
- Resolves actions and consequences
- Maintains the authoritative game state

The DM has final authority over all rules and outcomes.

The DM does NOT attempt to defeat players, but to challenge them and advance the story.

---

### 2.2 Player Agents

Each Player Agent:
- Controls exactly one character
- Declares intent, actions, and dialogue
- Acts only on its turn or when allowed to react
- Accepts dice outcomes and DM rulings
- Uses only in-character knowledge

Players must remain in character at all times.

---

## 3. Dice System

All uncertainty is resolved using virtual dice.

### 3.1 Dice Types

- d4: Minor damage or effects  
- d6: Common damage and checks  
- d8: Weapons and abilities  
- d10: Heavy or special damage  
- d12: Massive weapons  
- d20: Core resolution die  

### 3.2 Dice Notation

- 1d20 = roll one twenty-sided die  
- 2d6+3 = roll two six-sided dice and add 3  

### 3.3 Critical Results

- Natural 20 on d20: automatic success, enhanced effect
- Natural 1 on d20: automatic failure, complication possible

---

## 4. Core Attributes

Each character has six attributes, ranging from 3 to 18.

- Strength (STR): Physical power
- Dexterity (DEX): Agility and reflexes
- Constitution (CON): Endurance and health
- Intelligence (INT): Reasoning and memory
- Wisdom (WIS): Awareness and intuition
- Charisma (CHA): Social influence

### 4.1 Attribute Modifier

Modifier = floor((Attribute − 10) / 2)

Examples:
- 16 → +3  
- 12 → +1  
- 8 → −1  

---

## 5. Character Creation

### 5.1 Hit Points (HP)

Hit Points represent stamina and survivability.

HP = Class Base HP + Constitution Modifier

If HP is reduced to 0 or below, the character becomes incapacitated.

---

### 5.2 Armor Class (AC)

Armor Class determines how difficult a character is to hit.

AC = 10 + Armor Bonus + Dexterity Modifier

---

### 5.3 Classes

Each character selects one class.

- Fighter: Frontline melee combatant, high HP
- Wizard: Spellcaster, low HP
- Rogue: Stealth and precision, medium HP
- Cleric: Healing and support, medium HP

Classes define:
- Weapon proficiency
- Spell access
- Special abilities
- Progression features

---

## 6. Ability Checks and Skills

When an action has uncertain outcome, the DM requests a check.

Resolution formula:

1d20 + Attribute Modifier + Skill Bonus ≥ Difficulty Class (DC)

### Difficulty Classes

- DC 5: Trivial
- DC 10: Easy
- DC 15: Moderate
- DC 20: Hard
- DC 25+: Nearly impossible

---

## 7. Combat System

Combat is turn-based and structured.

### 7.1 Initiative

At the start of combat:

Initiative = 1d20 + Dexterity Modifier

Turns proceed from highest to lowest result.

---

### 7.2 Turn Structure

On their turn, a character may:
- Move
- Take one Action
- Take one Bonus Action (if available)
- Use one Reaction per round

Unused actions are lost.

---

### 7.3 Actions

Common actions include:
- Attack
- Cast Spell
- Dash
- Disengage
- Help
- Use Item

---

### 7.4 Attacking

Attack Roll:

1d20 + Attack Modifier

If the result equals or exceeds the target’s AC, the attack hits.

Damage:

Weapon Damage Dice + Strength or Dexterity Modifier

Damage reduces HP.

---

### 7.5 Death and Dying

If HP reaches 0 or below:
- Character is incapacitated
- Cannot take actions
- DM may require death saving throws
- Healing may stabilize the character

Permanent death is determined by the DM.

---

## 8. Magic System

### 8.1 Spellcasting Rules

Spells require:
- Available spell slots
- Valid targets
- Appropriate action economy

---

### 8.2 Spell Resolution

Either:
- Spell Attack: 1d20 + Spellcasting Modifier  
OR  
- Target Saving Throw: 1d20 + Relevant Attribute Modifier  

Spell descriptions determine which applies.

---

## 9. Roleplaying and Social Interaction

- Dialogue is free-form
- Rolls are only required when the outcome is uncertain
- Social interactions commonly use Charisma-based checks
- NPC reactions depend on tone, history, and context

Players must avoid meta-knowledge.

---

## 10. Exploration

Exploration includes:
- Travel
- Investigation
- Traps
- Environmental hazards
- Puzzles

The DM decides when checks are required and what information is available.

---

## 11. Alignment (Optional)

Alignment represents moral tendencies, not hard rules.

Axes:
- Lawful / Neutral / Chaotic
- Good / Neutral / Evil

Alignment influences narrative consequences and NPC perception.

---

## 12. Progression and Experience

The DM awards experience for:
- Combat
- Creative problem solving
- Roleplaying
- Story milestones

Leveling up may grant:
- Increased HP
- Improved abilities
- New spells or features

---

## 13. Rule Authority

Authority hierarchy:

Dungeon Master  
Written Rules  
Player Interpretation  

The DM may override rules for narrative consistency or balance.

---

## 14. AI Execution Rules

### 14.1 Player Agent Output

Each player response must include:
- Intent
- Action
- Optional dialogue

Example:
Intent: Distract the guard  
Action: Persuasion check  
Dialogue: "Long night, isn’t it?"

---

### 14.2 DM Output

Each DM response must include:
- Narrative outcome
- Dice results (if any)
- Updated game state
- Clear next prompt

Hidden rolls must be explicitly marked as hidden.

---

### 14.3 Determinism

- All dice rolls must be logged
- Game state must remain consistent
- No retroactive changes without narrative justification

---

## 15. Victory Conditions

There is no predefined win condition.

Success is defined by:
- Story completion
- Character survival
- Meaningful narrative development
- Satisfying conclusions

---

## End of Rules File
