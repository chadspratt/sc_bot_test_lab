from django.urls import path

from . import views

urlpatterns = [
    path('', views.match_list, name='match_list'),
    path('trigger-tests/', views.trigger_tests, name='trigger_tests'),
    path('replay/<int:match_id>/', views.serve_replay, name='serve_replay'),
    path('log/<int:match_id>/', views.serve_log, name='serve_log'),
    path('maps/', views.map_breakdown, name='map_breakdown'),
    path('buildings/', views.building_timing, name='building_timing'),
    path('utilities/', views.utilities, name='utilities'),
    path('utilities/recompile-cython/', views.recompile_cython, name='recompile_cython'),
    path('utilities/run-single-match/', views.run_single_match, name='run_single_match'),
]
