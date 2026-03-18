from django.urls import path

from . import views

urlpatterns = [
    path('', views.match_list, name='match_list'),
    path('trigger-tests/', views.trigger_tests, name='trigger_tests'),
    path('replay/<int:match_id>/', views.serve_replay, name='serve_replay'),
    path('log/<int:match_id>/', views.serve_log, name='serve_log'),
    path('log/<int:match_id>/bot/<str:bot_name>/', views.serve_aiarena_bot_log, name='serve_aiarena_bot_log'),
    path('maps/', views.map_breakdown, name='map_breakdown'),
    path('buildings/', views.building_timing, name='building_timing'),
    path('utilities/', views.utilities, name='utilities'),
    path('utilities/recompile-cython/', views.recompile_cython, name='recompile_cython'),
    path('utilities/run-single-match/', views.run_single_match, name='run_single_match'),
    path('utilities/run-custom-match/', views.run_custom_match, name='run_custom_match'),
    path('utilities/run-past-version-match/', views.run_past_version_match, name='run_past_version_match'),
    path('utilities/run-replay-match/', views.run_replay_match, name='run_replay_match'),
    path('utilities/test-suites/create/', views.create_test_suite, name='create_test_suite'),
    path('utilities/test-suites/<int:suite_id>/delete/', views.delete_test_suite, name='delete_test_suite'),
    path('custom-bots/', views.custom_bots, name='custom_bots'),
    path('custom-bots/create/', views.create_custom_bot, name='create_custom_bot'),
    path('custom-bots/<int:bot_id>/delete/', views.delete_custom_bot, name='delete_custom_bot'),
    path('custom-matches/', views.custom_match_list, name='custom_match_list'),
    path('api/trigger-tests/', views.api_trigger_tests, name='api_trigger_tests'),
    path('geometry/position-is-between/', views.position_is_between, name='position_is_between'),
]
