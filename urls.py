from django.urls import path

from . import views

urlpatterns = [
    # Top-level pages
    path('', views.results_page, name='results'),
    path('run-match/', views.run_match_page, name='run_match'),
    path('config/', views.config_page, name='config_page'),
    path('custom/', views.custom_page, name='custom_page'),

    # First-run setup
    path('setup/', views.setup_page, name='setup_page'),
    path('setup/save/', views.save_setup, name='save_setup'),

    # Results actions
    path('trigger-tests/', views.trigger_tests, name='trigger_tests'),

    # Serve files
    path('replay/<int:match_id>/', views.serve_replay, name='serve_replay'),
    path('log/<int:match_id>/', views.serve_log, name='serve_log'),
    path('log/<int:match_id>/bot/<str:bot_name>/', views.serve_aiarena_bot_log, name='serve_aiarena_bot_log'),

    # Run match actions
    path('run-match/single/', views.run_single_match, name='run_single_match'),
    path('run-match/custom/', views.run_custom_match, name='run_custom_match'),
    path('run-match/past-version/', views.run_past_version_match, name='run_past_version_match'),
    path('run-match/replay/', views.run_replay_match, name='run_replay_match'),
    path('run-match/replay-test/', views.run_saved_replay_test, name='run_saved_replay_test'),

    # Config actions
    path('config/custom-bots/create/', views.create_custom_bot, name='create_custom_bot'),
    path('config/custom-bots/<int:bot_id>/delete/', views.delete_custom_bot, name='delete_custom_bot'),
    path('config/custom-bots/<int:bot_id>/test-suite/', views.update_custom_bot_test_suite, name='update_custom_bot_test_suite'),
    path('config/custom-bots/<int:bot_id>/test-subject/', views.update_custom_bot_test_subject, name='update_custom_bot_test_subject'),
    path('config/test-suites/create/', views.create_test_suite, name='create_test_suite'),
    path('config/test-suites/<int:suite_id>/update/', views.update_test_suite, name='update_test_suite'),
    path('config/test-suites/<int:suite_id>/delete/', views.delete_test_suite, name='delete_test_suite'),
    path('config/custom-bots/<int:bot_id>/active/', views.update_custom_bot_active, name='update_custom_bot_active'),
    path('config/replay-tests/create/', views.create_replay_test, name='create_replay_test'),
    path('config/replay-tests/<int:test_id>/delete/', views.delete_replay_test, name='delete_replay_test'),
    path('config/system/', views.update_system_config, name='update_system_config'),
    path('config/browse-path/', views.browse_path, name='browse_path'),
    path('config/prompt-templates/create/', views.create_prompt_template, name='create_prompt_template'),
    path('config/prompt-templates/<int:template_id>/update/', views.update_prompt_template, name='update_prompt_template'),
    path('config/prompt-templates/<int:template_id>/delete/', views.delete_prompt_template, name='delete_prompt_template'),
    path('api/template-file-content/', views.get_template_file_content, name='get_template_file_content'),

    # Custom actions — plugin dispatcher
    path('custom/plugin/<str:plugin_name>/', views.run_plugin, name='run_plugin'),
    path('custom/<str:plugin_name>/', views.custom_plugin_page, name='custom_plugin_page'),

    # API
    path('api/trigger-tests/', views.api_trigger_tests, name='api_trigger_tests'),
    path('api/trigger-ticket-tests/', views.api_trigger_ticket_tests, name='api_trigger_ticket_tests'),

    # Tickets
    path('tickets/', views.tickets_page, name='tickets'),
    path('tickets/<int:ticket_id>/', views.ticket_detail_page, name='ticket_detail'),
    path('tickets/create/', views.create_ticket, name='create_ticket'),
    path('tickets/<int:ticket_id>/update/', views.update_ticket, name='update_ticket'),
    path('tickets/<int:ticket_id>/status/', views.update_ticket_status, name='update_ticket_status'),
    path('tickets/<int:ticket_id>/generate-prompt/', views.generate_ticket_prompt, name='generate_ticket_prompt'),
    path('tickets/<int:ticket_id>/run-tests/', views.run_ticket_tests, name='run_ticket_tests'),
    path('tickets/<int:ticket_id>/delete/', views.delete_ticket, name='delete_ticket'),
    path('tickets/<int:ticket_id>/branches/', views.list_branches, name='list_branches'),
    path('tickets/<int:ticket_id>/merge/', views.merge_branch, name='merge_branch'),

    # Geometry (standalone tool page)
    path('geometry/position-is-between/', views.position_is_between, name='position_is_between'),
]
