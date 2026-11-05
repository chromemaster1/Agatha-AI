import inspect
import tempfile
import unittest
from unittest.mock import Mock, patch

import main_agent
import tools_creator
import tools_music


class ExpandedFeatureTests(unittest.TestCase):
    def test_tutoring_requests_get_adaptive_subject_guidance(self):
        prompt = main_agent._compose_system_prompt("base prompt", "Teach me chemistry", False)

        self.assertIn("mathematics, English, chemistry, physics, biology", prompt)
        self.assertIn("worked examples", prompt)
        self.assertIn("do not claim to retrain", prompt)

    def test_base_conversation_prompt_guides_punctuation_for_speech(self):
        prompt = inspect.getsource(main_agent.run_agent_loop)
        self.assertIn("correct, natural punctuation", prompt)
        self.assertIn("punctuation creates clear pauses", prompt)

    def test_stunning_3d_image_request_routes_with_render_intent(self):
        request = "Create a stunning 3D image of a glass octopus in a coral garden"
        with patch.object(main_agent.tools_creator, "generate_image", return_value="generated") as generate:
            result = main_agent._parse_creator(request, request.lower())

        self.assertEqual(result, "generated")
        generate.assert_called_once_with("3D render of a glass octopus in a coral garden")

    def test_3d_prompt_receives_render_direction(self):
        image_generator = Mock()
        image_generator.return_value = b"image-data"
        hooks = {**tools_creator._hooks, "image": image_generator}
        with tempfile.TemporaryDirectory() as output_dir, \
                patch.object(tools_creator, "OUTPUT_DIR", output_dir), \
                patch.object(tools_creator, "_hooks", hooks), \
                patch.object(tools_creator, "record_artifact", return_value="image-1"):
            result = tools_creator.generate_image("3D render of a glass octopus")

        self.assertIn("/artifact/image-1", result)
        generated_prompt = image_generator.call_args.args[0]
        self.assertIn("physically based materials", generated_prompt)
        self.assertIn("cinematic", generated_prompt)

    def test_music_file_playback_calls_native_player_with_validated_path(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio_file, \
                patch.object(tools_music, "run_applescript") as run_script:
            result = tools_music.play_music(file_path=audio_file.name)

        self.assertIn("Playing", result)
        run_script.assert_called_once()
        self.assertIn("open POSIX file", run_script.call_args.args[0])
        self.assertEqual(run_script.call_args.args[1], audio_file.name)


if __name__ == "__main__":
    unittest.main()