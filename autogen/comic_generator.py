"""
Comic Generator Module
-----------------------
Converts D&D game story messages into comic panels by:
1. Extracting key narrative moments from the story feed
2. Using the LLM to generate image prompts for each panel
3. Calling the local image generation service to create panel images
4. Assembling panels into a comic strip layout

Usage:
    generator = ComicGenerator(ollama_url="http://ollama:11434", image_service_url="http://image-gen:8090")
    comic = await generator.generate_comic(story_messages, game_id)
"""

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ComicPanel:
    """A single comic panel with image and caption."""
    panel_id: str
    panel_number: int
    image_prompt: str
    caption: str
    speaker: str
    dialogue: str = ""
    image_url: Optional[str] = None
    image_filename: Optional[str] = None
    status: str = "pending"  # pending | generating | done | error
    error: Optional[str] = None
    scene_id: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class ComicPage:
    """A page of comic panels."""
    page_number: int
    panels: list[ComicPanel] = field(default_factory=list)
    title: str = ""

    def to_dict(self):
        return {
            "page_number": self.page_number,
            "title": self.title,
            "panels": [p.to_dict() for p in self.panels],
        }


@dataclass
class Comic:
    """A complete comic with multiple pages."""
    comic_id: str
    game_id: str
    title: str = "The Crypt of the Shadow Lord"
    pages: list[ComicPage] = field(default_factory=list)
    status: str = "pending"  # pending | generating | done | error
    created_at: str = ""
    style: str = "comic"
    total_panels: int = 0
    generated_panels: int = 0

    def to_dict(self):
        return {
            "comic_id": self.comic_id,
            "game_id": self.game_id,
            "title": self.title,
            "pages": [p.to_dict() for p in self.pages],
            "status": self.status,
            "created_at": self.created_at,
            "style": self.style,
            "total_panels": self.total_panels,
            "generated_panels": self.generated_panels,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Story filtering — extract narrative-worthy messages
# ──────────────────────────────────────────────────────────────────────────────

STORY_AGENTS = {"DungeonMaster", "Thorin", "Elara", "Shadow", "Aldric"}

# Character descriptions for consistent image generation
CHARACTER_DESCRIPTIONS = {
    "DungeonMaster": "",  # DM doesn't appear physically
    "Thorin": "a stout dwarven fighter with thick brown beard, chain mail armor, shield, wielding a battleaxe",
    "Elara": "a slender elven wizard with long silver hair, flowing blue robes, holding a glowing staff",
    "Shadow": "a small halfling rogue in dark leather armor, hooded cloak, dual daggers, sneaky expression",
    "Aldric": "a tall human cleric in white and gold robes, holy symbol on chain, warm determined expression",
}

SCENE_DESCRIPTIONS = {
    0: "a quaint medieval village with thatched-roof cottages, a stone well in the center, warm sunset lighting",
    1: "a dark crypt entrance with crumbling stone archway, moss-covered stairs leading down, eerie green torchlight, skeleton guards",
    2: "a vast underground chamber with dark purple crystals, a shadowy throne, ominous mist, the Shadow Lord on a dark throne",
}


def is_story_worthy(msg: dict) -> bool:
    """Check if a message should be included in the comic."""
    name = msg.get("name", "")
    if name not in STORY_AGENTS:
        return False
    content = (msg.get("content") or "").strip()
    if not content:
        return False
    if re.match(r'^\[tool call:', content, re.IGNORECASE):
        return False
    if re.match(r'^[\[{]', content):
        return False
    if len(content) < 30:
        return False
    return True


def extract_story_messages(messages: list[dict]) -> list[dict]:
    """Filter messages to only story-worthy narrative content."""
    return [m for m in messages if is_story_worthy(m)]


def clean_story_text(raw: str) -> str:
    """Strip tool-call artifacts from narrative text."""
    lines = raw.split('\n')
    cleaned = [
        line for line in lines
        if not re.match(r'^\s*[\[{]', line)
        and not re.match(r'^\[tool call:', line.strip(), re.IGNORECASE)
    ]
    return '\n'.join(cleaned).strip()


# ──────────────────────────────────────────────────────────────────────────────
# Comic Generator
# ──────────────────────────────────────────────────────────────────────────────

class ComicGenerator:
    """Generates comic panels from D&D story messages."""

    def __init__(
        self,
        ollama_url: str = None,
        image_service_url: str = None,
        style: str = "comic",
        panels_per_page: int = 4,
        max_panels: int = 8,
    ):
        self.ollama_url = ollama_url or os.environ.get("OLLAMA_HOST", "http://ollama:11434")
        self.image_service_url = image_service_url or os.environ.get("IMAGE_SERVICE_URL", "http://image-gen:8090")
        self.style = style
        self.panels_per_page = panels_per_page
        self.max_panels = max_panels
        self._client = httpx.AsyncClient(timeout=300.0)

    async def close(self):
        await self._client.aclose()

    # ── LLM: Extract panel descriptions from story ─────────────────────────

    async def _call_llm(self, prompt: str, system: str = "") -> str:
        """Call Ollama LLM to process text."""
        url = f"{self.ollama_url}/api/chat"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = await self._client.post(url, json={
                "model": "llama3.1:8b",
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": 2000},
            })
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")
        except Exception as exc:
            print(f"[comic] LLM call failed: {exc}")
            return ""

    async def extract_panels(self, story_messages: list[dict], game_id: str) -> list[dict]:
        """
        Use the LLM to break the story into comic panels.
        Returns a list of panel descriptions with prompts and captions.
        """
        # Combine story text
        story_text = ""
        for msg in story_messages:
            name = msg.get("name", "Unknown")
            content = clean_story_text(msg.get("content", ""))
            if content:
                story_text += f"\n[{name}]: {content}\n"

        if not story_text.strip():
            return []

        # Trim to fit context
        if len(story_text) > 6000:
            story_text = story_text[:6000] + "\n... (story continues)"

        system_prompt = """You are a comic book artist planning panel layouts for a D&D adventure comic.
You must output ONLY valid JSON — no extra text, no markdown fences, no explanation.

Output a JSON array of panel objects. Each panel has:
- "panel_number": sequential integer starting at 1
- "scene_description": a vivid visual description for an image generator (describe setting, characters, action, lighting, mood — NO dialogue)
- "caption": short narrative caption for the panel (1-2 sentences)
- "speaker": which character is featured (DungeonMaster for scene-setting panels, or character name)
- "dialogue": a short speech bubble quote if a character is speaking, or empty string
- "scene_id": 0 for village, 1 for crypt entrance, 2 for shadow lord's chamber

Important rules:
- Create 4-8 panels covering the key dramatic moments
- scene_description should be purely VISUAL — describe what we SEE, not what we hear
- Include character appearances in scene descriptions (e.g. "a dwarven fighter in chain mail armor")
- Focus on dramatic moments: entering the crypt, combat with skeletons, the final battle
- Each panel should be a distinct visual moment"""

        user_prompt = f"""Break this D&D adventure story into comic panels:

{story_text}

Output ONLY a JSON array of panel objects. No markdown, no extra text."""

        response = await self._call_llm(user_prompt, system_prompt)

        # Parse the LLM response
        panels = self._parse_panels_response(response)
        return panels[:self.max_panels]

    def _parse_panels_response(self, response: str) -> list[dict]:
        """Parse the LLM's panel descriptions from its response."""
        # Try to extract JSON array from response
        response = response.strip()

        # Remove markdown code fences if present
        response = re.sub(r'^```(?:json)?\s*', '', response)
        response = re.sub(r'\s*```$', '', response)

        # Try direct parse
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "panels" in data:
                return data["panels"]
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in the text
        match = re.search(r'\[[\s\S]*\]', response)
        if match:
            try:
                data = json.loads(match.group())
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass

        print(f"[comic] Failed to parse panel response, using fallback")
        return self._fallback_panels()

    def _fallback_panels(self) -> list[dict]:
        """Generate fallback panels if LLM parsing fails."""
        return [
            {
                "panel_number": 1,
                "scene_description": "A medieval village at sunset, four adventurers gathered around a stone well discussing their quest",
                "caption": "The party gathers in the Village of Millhaven...",
                "speaker": "DungeonMaster",
                "dialogue": "",
                "scene_id": 0,
            },
            {
                "panel_number": 2,
                "scene_description": "A dwarven fighter raising his battleaxe, rallying the party with determination",
                "caption": "Thorin rallies the party with a war cry.",
                "speaker": "Thorin",
                "dialogue": "To the crypt! We shall vanquish this evil!",
                "scene_id": 0,
            },
            {
                "panel_number": 3,
                "scene_description": "Dark crypt entrance with crumbling stone steps leading underground, eerie green light emanating from below",
                "caption": "The party descends into the Crypt...",
                "speaker": "DungeonMaster",
                "dialogue": "",
                "scene_id": 1,
            },
            {
                "panel_number": 4,
                "scene_description": "Two skeleton guards with rusted swords lunging forward in a dark crypt corridor, bones gleaming in torchlight",
                "caption": "Skeleton guards block the path!",
                "speaker": "DungeonMaster",
                "dialogue": "",
                "scene_id": 1,
            },
            {
                "panel_number": 5,
                "scene_description": "An elven wizard casting a fire bolt spell, orange flames streaking from her hands toward the skeletons",
                "caption": "Elara unleashes her arcane power!",
                "speaker": "Elara",
                "dialogue": "Feel the burn of arcane fire!",
                "scene_id": 1,
            },
            {
                "panel_number": 6,
                "scene_description": "A vast underground chamber with purple crystals, a shadowy figure on a dark throne, ominous mist swirling",
                "caption": "The Shadow Lord awaits in his dark chamber...",
                "speaker": "DungeonMaster",
                "dialogue": "",
                "scene_id": 2,
            },
            {
                "panel_number": 7,
                "scene_description": "Epic battle scene, four adventurers fighting a dark spectral figure surrounded by purple energy, dramatic lighting",
                "caption": "The final battle begins!",
                "speaker": "DungeonMaster",
                "dialogue": "",
                "scene_id": 2,
            },
            {
                "panel_number": 8,
                "scene_description": "Victorious adventurers standing over the defeated Shadow Lord, light breaking through the crypt ceiling, triumphant poses",
                "caption": "The Shadow Lord is defeated! Victory!",
                "speaker": "DungeonMaster",
                "dialogue": "",
                "scene_id": 2,
            },
        ]

    # ── Image Generation ───────────────────────────────────────────────────

    async def _generate_image(self, prompt: str, panel_number: int) -> dict:
        """Call the image generation service to create a panel image."""
        # Enhance prompt with D&D/fantasy context
        enhanced_prompt = (
            f"D&D fantasy adventure scene, {prompt}, "
            f"dramatic lighting, detailed background, epic composition"
        )

        try:
            resp = await self._client.post(
                f"{self.image_service_url}/generate",
                json={
                    "prompt": enhanced_prompt,
                    "style": self.style,
                    "width": 512,
                    "height": 512,
                    "num_inference_steps": 1,
                    "guidance_scale": 0.0,
                    "seed": panel_number * 42,  # deterministic seeds per panel
                },
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"[comic] Image generation failed for panel {panel_number}: {exc}")
            return {"error": str(exc)}

    # ── Main generation pipeline ───────────────────────────────────────────

    async def generate_comic(
        self,
        messages: list[dict],
        game_id: str,
        title: str = "The Crypt of the Shadow Lord",
        progress_callback=None,
    ) -> Comic:
        """
        Full pipeline: story messages → panel descriptions → images → comic.

        Args:
            messages: Raw game messages
            game_id: The game ID
            title: Comic title
            progress_callback: async callable(comic) called after each panel is generated
        """
        comic_id = str(uuid.uuid4())[:8]
        comic = Comic(
            comic_id=comic_id,
            game_id=game_id,
            title=title,
            style=self.style,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # Step 1: Extract story messages
        story_msgs = extract_story_messages(messages)
        if not story_msgs:
            comic.status = "error"
            return comic

        # Step 2: Use LLM to plan panels
        comic.status = "generating"
        if progress_callback:
            await progress_callback(comic)

        panel_descriptions = await self.extract_panels(story_msgs, game_id)
        if not panel_descriptions:
            panel_descriptions = self._fallback_panels()

        # Step 3: Create panel objects and organize into pages
        panels = []
        for i, desc in enumerate(panel_descriptions):
            panel = ComicPanel(
                panel_id=f"{comic_id}-p{i+1}",
                panel_number=i + 1,
                image_prompt=desc.get("scene_description", ""),
                caption=desc.get("caption", ""),
                speaker=desc.get("speaker", "DungeonMaster"),
                dialogue=desc.get("dialogue", ""),
                scene_id=desc.get("scene_id", 0),
            )
            panels.append(panel)

        comic.total_panels = len(panels)

        # Organize into pages
        for page_num in range(0, len(panels), self.panels_per_page):
            page_panels = panels[page_num:page_num + self.panels_per_page]
            page = ComicPage(
                page_number=(page_num // self.panels_per_page) + 1,
                panels=page_panels,
                title=f"Page {(page_num // self.panels_per_page) + 1}",
            )
            comic.pages.append(page)

        if progress_callback:
            await progress_callback(comic)

        # Step 4: Generate images for each panel
        for page in comic.pages:
            for panel in page.panels:
                panel.status = "generating"
                if progress_callback:
                    await progress_callback(comic)

                result = await self._generate_image(panel.image_prompt, panel.panel_number)

                if "error" in result:
                    panel.status = "error"
                    panel.error = result["error"]
                else:
                    panel.image_url = result.get("url", "")
                    panel.image_filename = result.get("filename", "")
                    panel.status = "done"

                comic.generated_panels += 1
                if progress_callback:
                    await progress_callback(comic)

        comic.status = "done"
        if progress_callback:
            await progress_callback(comic)

        return comic

    async def check_image_service(self) -> bool:
        """Check if the image generation service is available."""
        try:
            resp = await self._client.get(f"{self.image_service_url}/health")
            return resp.status_code == 200
        except Exception:
            return False
