import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main_agent


def _tool(name):
    return {"type": "function", "function": {"name": name}}


class ToolSelectionTests(unittest.TestCase):
    def setUp(self):
        self.available_tools = [
            _tool("add_todo"),
            _tool("list_todos"),
            _tool("schedule_meeting"),
            _tool("summarize_emails"),
        ]

    def test_conversation_does_not_send_tool_schemas(self):
        selected = main_agent._select_tools_for_request(
            "Hi, how are you?", self.available_tools
        )

        self.assertEqual(selected, [])

    def test_specific_intent_selects_only_related_tools(self):
        selected = main_agent._select_tools_for_request(
            "Please add a task to my to-do list", self.available_tools
        )

        self.assertEqual(
            [tool["function"]["name"] for tool in selected],
            ["add_todo", "list_todos"],
        )

    def test_ambiguous_action_keeps_full_tool_set(self):
        selected = main_agent._select_tools_for_request(
            "Please add this", self.available_tools
        )

        self.assertEqual(selected, self.available_tools)


class GreetingRoutingTests(unittest.TestCase):
    def test_plain_greeting_gets_short_local_response(self):
        self.assertEqual(
            main_agent._parse_greeting("Hello!", "hello!"),
            "Hello! How can I assist you today?",
        )

    def test_greeting_prefix_with_request_is_not_intercepted(self):
        request = "Hello, can you summarize my email?"
        self.assertIsNone(main_agent._parse_greeting(request, request.lower()))

    def test_explicit_capabilities_request_still_lists_functions(self):
        request = "List all your functions"
        result = main_agent._parse_capabilities(request, request.lower())
        self.assertIsNotNone(result)
        self.assertIn("Calendar", result)

    def test_startup_greeting_varies_by_time_and_uses_saved_name(self):
        profile = {"name": "Avery", "interaction_count": 4}
        self.assertIn("Good morning, Avery", main_agent._initial_greeting(datetime(2026, 11, 4, 9), profile))
        self.assertIn("Good afternoon, Avery", main_agent._initial_greeting(datetime(2026, 11, 4, 14), profile))
        self.assertIn("Pulling an all-nighter, Avery", main_agent._initial_greeting(datetime(2026, 11, 4, 2), profile))

    def test_wake_word_greeting_stays_short(self):
        self.assertEqual(main_agent._wake_greeting(), "Hello. How can I assist you today?")


class MeetingParserTests(unittest.TestCase):
    def test_month_name_date_is_parsed_and_scheduled(self):
        request = "Schedule a meeting with Alex on October 24th, 2026 at 3pm"
        with patch.object(
            main_agent, "schedule_meeting", return_value="scheduled"
        ) as schedule:
            result = main_agent._parse_meeting(request, request.lower())

        self.assertEqual(result, "scheduled")
        self.assertEqual(schedule.call_args.kwargs["title"], "Meeting with Alex")
        self.assertEqual(schedule.call_args.kwargs["date_str"], "2026-10-24 15:00")

    def test_date_from_recent_user_turn_is_reused(self):
        history = [
            {"role": "user", "content": "Let's use October 24, 2026 for this."},
            {"role": "assistant", "content": "Got it."},
        ]
        request = "Schedule a meeting with Alex at 3pm"
        with patch.object(
            main_agent, "schedule_meeting", return_value="scheduled"
        ) as schedule:
            result = main_agent._parse_meeting(request, request.lower(), history)

        self.assertEqual(result, "scheduled")
        self.assertEqual(schedule.call_args.kwargs["date_str"], "2026-10-24 15:00")

    def test_unrelated_older_date_is_not_silently_reused(self):
        history = [
            {"role": "user", "content": "Let's use October 24, 2026 for the launch."},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "I also have a dentist appointment soon."},
        ]
        request = "Schedule a meeting with Alex at 3pm"
        with patch.object(main_agent, "schedule_meeting", return_value="scheduled") as schedule:
            result = main_agent._parse_meeting(request, request.lower(), history)

        self.assertIn("What date", result)
        schedule.assert_not_called()

    def test_explicit_reference_can_reuse_older_date(self):
        history = [
            {"role": "user", "content": "Let's use October 24, 2026 for this."},
            {"role": "assistant", "content": "Got it."},
            {"role": "user", "content": "I also have a dentist appointment soon."},
        ]
        request = "Schedule a meeting with Alex at 3pm on that date"
        with patch.object(main_agent, "schedule_meeting", return_value="scheduled") as schedule:
            result = main_agent._parse_meeting(request, request.lower(), history)

        self.assertEqual(result, "scheduled")
        self.assertEqual(schedule.call_args.kwargs["date_str"], "2026-10-24 15:00")

    def test_local_dispatcher_passes_and_extends_conversation(self):
        history = [{"role": "user", "content": "Let's use October 24, 2026."}]
        request = "Schedule a meeting with Alex at 3pm"
        with patch.object(main_agent, "schedule_meeting", return_value="scheduled") as schedule, \
                patch.object(main_agent, "speak"), \
                patch.object(main_agent.tools_memory, "log_event"):
            handled = main_agent.parse_and_execute_simple_command(request, history)

        self.assertTrue(handled)
        self.assertEqual(schedule.call_args.kwargs["date_str"], "2026-10-24 15:00")
        self.assertEqual(history[-2]["content"], request)
        self.assertEqual(history[-1]["content"], "scheduled")


class AttachmentMessageTests(unittest.TestCase):
    def test_openai_and_ollama_receive_uploaded_image_data(self):
        messages = [{
            "role": "user",
            "content": "What is in this picture?",
            "attachments": [{"mime_type": "image/png", "image_base64": "aW1hZ2U="}],
        }]

        openai_message = main_agent._messages_for_openai(messages)[0]
        ollama_message = main_agent._messages_for_ollama(messages)[0]

        self.assertEqual(openai_message["content"][1]["image_url"]["url"], "data:image/png;base64,aW1hZ2U=")
        self.assertEqual(ollama_message["images"], ["aW1hZ2U="])
        self.assertNotIn("attachments", ollama_message)

    def test_anthropic_receives_uploaded_image_block(self):
        system, messages = main_agent._messages_to_anthropic([
            {"role": "system", "content": "instructions"},
            {
                "role": "user",
                "content": "Describe this image.",
                "attachments": [{"mime_type": "image/jpeg", "image_base64": "aW1hZ2U="}],
            },
        ])

        self.assertEqual(system, "instructions")
        self.assertEqual(messages[0]["content"][1]["type"], "image")
        self.assertEqual(messages[0]["content"][1]["source"]["data"], "aW1hZ2U=")


class UploadedDocumentSummaryTests(unittest.TestCase):
    def test_long_document_is_summarized_in_bounded_sections(self):
        calls = []

        def summarize(system, prompt, tier="main"):
            calls.append((system, prompt, tier))
            return "section notes" if tier == "light" else "Final document summary"

        attachment = {"name": "book.txt", "text": "A useful passage. " * 1000}
        with patch.object(main_agent, "_plain_llm", side_effect=summarize):
            result = main_agent._summarize_uploaded_documents("Summarize this book", [attachment])

        self.assertEqual(result, "Final document summary")
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(len(prompt) <= 16080 for _, prompt, _ in calls[:2]))
        self.assertEqual(calls[-1][2], "main")

    def test_document_is_not_summarized_without_summary_intent(self):
        with patch.object(main_agent, "_plain_llm") as summarize:
            result = main_agent._summarize_uploaded_documents(
                "What is the author's name?", [{"name": "book.txt", "text": "The author's name is Jo."}]
            )

        self.assertIsNone(result)
        summarize.assert_not_called()


class AuthenticationSpeechGateTests(unittest.TestCase):
    def test_speak_is_silent_before_dashboard_authentication(self):
        main_agent.dashboard.authenticated_event.clear()
        try:
            with patch.object(main_agent, "_voice_speak") as voice, \
                    patch.object(main_agent.dashboard, "add_log") as add_log, \
                    patch.object(main_agent.tools_profile, "log_turn") as log_turn:
                main_agent.speak("This must wait for sign-in.")

            voice.assert_not_called()
            add_log.assert_not_called()
            log_turn.assert_not_called()
        finally:
            main_agent.dashboard.authenticated_event.set()

    def test_council_leaves_answer_alone_without_two_linked_providers(self):
        with patch.object(main_agent, "_linked_provider_ids", return_value=["openai"]), \
                patch.object(main_agent, "_configured_provider_clients") as clients:
            answer = main_agent._review_with_agent_council("Explain this", "A concise answer.")

        self.assertEqual(answer, "A concise answer.")
        clients.assert_not_called()

    def test_council_collects_review_from_multiple_linked_models(self):
        approved = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="APPROVED"))])
        openai_client = Mock()
        gemini_client = Mock()
        openai_client.chat.completions.create.return_value = approved
        gemini_client.chat.completions.create.return_value = approved
        with patch.object(main_agent, "_linked_provider_ids", return_value=["openai", "gemini"]), \
                patch.object(main_agent, "_configured_provider_clients",
                             return_value={"openai": openai_client, "gemini": gemini_client}), \
                patch.object(main_agent, "_provider_model", return_value="test-model"):
            answer = main_agent._review_with_agent_council("Explain this", "A concise answer.")

        self.assertEqual(answer, "A concise answer.")
        openai_client.chat.completions.create.assert_called_once()
        gemini_client.chat.completions.create.assert_called_once()


class CreatorClipRoutingTests(unittest.TestCase):
    def test_clip_request_without_source_asks_for_video_not_generation_provider(self):
        request = "Generate 30 second clips and sync them with my song"
        with patch.object(main_agent.tools_creator, "generate_video") as generate:
            result = main_agent._parse_creator(request, request.lower())

        self.assertIn("Send the video URL or local file path", result)
        generate.assert_not_called()

    def test_video_url_reply_resumes_pending_clip_extraction(self):
        video_url = "https://youtu.be/example123"
        song_path = "/tmp/song.mp3"
        history = [{
            "role": "user",
            "content": "Create 30 second clips from a video and sync them with my song",
            "attachments": [{"mime_type": "audio/mpeg", "path": song_path}],
        }, {"role": "assistant", "content": "Send me the video URL."}]
        with patch.object(main_agent.tools_creator, "generate_video") as generate, \
                patch.object(main_agent.tools_creator, "create_clips", return_value="clips ready") as create:
            result = main_agent._parse_creator(f"Here is the video URL {video_url}",
                                               f"here is the video url {video_url}", history)

        self.assertEqual(result, "clips ready")
        generate.assert_not_called()
        create.assert_called_once()
        self.assertEqual(create.call_args.args[0], video_url)
        self.assertEqual(create.call_args.kwargs["music_path"], song_path)

    def test_todo_items_that_mention_video_are_not_creator_requests(self):
        request = ("Create a to-do list with 3 things, 1 create Ventara video, "
                   "2 review and improve code base, 3 workout and rest")
        with patch.object(main_agent.tools_creator, "generate_video") as generate, \
            patch.object(main_agent, "add_todo", return_value="added") as add_todo, \
                patch.object(main_agent, "speak"), \
                patch.object(main_agent.tools_memory, "log_event"):
            handled = main_agent.parse_and_execute_simple_command(request)

        self.assertTrue(handled)
        generate.assert_not_called()
        self.assertEqual(add_todo.call_count, 3)

    def test_prompt_video_without_source_routes_to_generator(self):
        request = "Create a 15 second video about a red sports car, synced to 128 BPM"
        with patch.object(main_agent.tools_creator, "generate_video", return_value="generated") as generate:
            result = main_agent._parse_creator(request, request.lower())

        self.assertEqual(result, "generated")
        generate.assert_called_once_with(
            "a red sports car, synced to 128 BPM", music_path="", music_url="", duration=15, bpm=128,
        )

    def test_attached_song_reply_resumes_pending_video_generation(self):
        song_path = "/tmp/goochi-Darling.mp3"
        history = [
            {"role": "user", "content": (
                "Can you generate an animated video that relates to my song/lyrics and sync the video, "
                "so i can download and use as short form promotional content, each video should be at-least 30 sec?"
            )},
            {"role": "assistant", "content": "Attach a song or send its link so I can sync the generated video to its beat."},
        ]
        attachments = [{"name": "goochi-Darling.mp3", "path": song_path, "mime_type": "audio/mpeg"}]

        with patch.object(main_agent.tools_creator, "generate_video", return_value="generated") as generate:
            result = main_agent._parse_creator("", "", history, attachments)

        self.assertEqual(result, "generated")
        generate.assert_called_once_with(
            "an animated video that relates to my song/lyrics and sync the video",
            music_path=song_path, music_url="", duration=30, bpm=120,
        )
        self.assertTrue(main_agent._is_video_music_upload("", attachments, history))

    def test_image_request_routes_to_image_generator(self):
        request = "Generate an image of a lighthouse in a storm"
        with patch.object(main_agent.tools_creator, "generate_image", return_value="generated") as generate:
            result = main_agent._parse_creator(request, request.lower())

        self.assertEqual(result, "generated")
        generate.assert_called_once_with("a lighthouse in a storm")

    def test_sync_request_waits_for_song_instead_of_making_unsynced_clips(self):
        request = ('cut out 30secs clips from this video url '
                   'https://youtu.be/HFAzgHHITVM?si=sample and sync it with my song')
        with patch.object(main_agent.tools_creator, "create_clips") as create:
            result = main_agent._parse_creator(request, request.lower())

        self.assertIn("Send the song link", result)
        create.assert_not_called()

    def test_song_link_reply_resumes_clip_request_with_both_urls(self):
        video_url = "https://youtu.be/HFAzgHHITVM?si=sample"
        song_url = "https://music.youtube.com/watch?v=Av0QC8LyjaE"
        history = [{
            "role": "user",
            "content": f"cut out 30secs clips from this video url {video_url} and sync it with my song",
        }, {"role": "assistant", "content": "Send the song link."}]
        with patch.object(main_agent.tools_creator, "create_clips", return_value="finished") as create:
            result = main_agent._parse_creator(song_url, song_url.lower(), history)

        self.assertEqual(result, "finished")
        self.assertEqual(create.call_args.args[0], video_url)
        self.assertEqual(create.call_args.kwargs["music_url"], song_url)
        self.assertEqual(create.call_args.kwargs["clip_seconds"], 30)
        self.assertEqual(create.call_args.kwargs["num_clips"], 3)

    def test_video_url_and_attached_song_resume_original_clip_request(self):
        video_url = "https://youtu.be/HFAzgHHITVM?si=sample"
        song_path = "/tmp/Goochi_Cold war.mp3"
        history = [{
            "role": "user",
            "content": "Can you generate short 15sec clips from a url and sync them with my song?",
        }]
        attachments = [{"name": "Goochi_Cold war.mp3", "path": song_path, "mime_type": "audio/mpeg"}]
        with patch.object(main_agent.tools_creator, "create_clips", return_value="finished") as create:
            result = main_agent._parse_creator(video_url, video_url.lower(), history, attachments)

        self.assertEqual(result, "finished")
        self.assertEqual(create.call_args.args[0], video_url)
        self.assertEqual(create.call_args.kwargs["music_path"], song_path)
        self.assertEqual(create.call_args.kwargs["clip_seconds"], 15)


class OllamaGenerationBudgetTests(unittest.TestCase):
    def test_chat_uses_short_output_budget(self):
        self.assertEqual(main_agent._ollama_output_budget([]), 120)

    def test_tool_selection_has_room_to_finish(self):
        self.assertEqual(main_agent._ollama_output_budget([_tool("add_todo")]), 320)

    def test_tool_free_long_form_tasks_keep_large_budget(self):
        self.assertEqual(main_agent._ollama_output_budget(None), 1024)


class ConversationHistoryTests(unittest.TestCase):
    def test_keeps_system_prompt_and_complete_recent_tool_turns(self):
        tool_call_message = SimpleNamespace(role="assistant", tool_calls=["call"])
        messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "recent request"},
            tool_call_message,
            {"role": "tool", "content": "tool result"},
            {"role": "assistant", "content": "recent reply"},
        ]

        trimmed = main_agent._trim_conversation_history(messages, max_turns=1)

        self.assertEqual(trimmed[0], messages[0])
        self.assertEqual(trimmed[1:], messages[3:])


if __name__ == "__main__":
    unittest.main()
