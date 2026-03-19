from django.urls import path

from . import views

urlpatterns = [
    # Top-level pages
    path('', views.results_page, name='results'),
    path('run-match/', views.run_match_page, name='run_match'),
    path('config/', views.config_page, name='config_page'),
    path('custom/', views.custom_page, name='custom_page'),

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
    path('config/test-suites/create/', views.create_test_suite, name='create_test_suite'),
    path('config/test-suites/<int:suite_id>/delete/', views.delete_test_suite, name='delete_test_suite'),
    path('config/replay-tests/create/', views.create_replay_test, name='create_replay_test'),
    path('config/replay-tests/<int:test_id>/delete/', views.delete_replay_test, name='delete_replay_test'),

    # Custom actions
    path('custom/recompile-cython/', views.recompile_cython, name='recompile_cython'),

    # API
    path('api/trigger-tests/', views.api_trigger_tests, name='api_trigger_tests'),

    # Geometry (standalone tool page)
    path('geometry/position-is-between/', views.position_is_between, name='position_is_between'),
]
