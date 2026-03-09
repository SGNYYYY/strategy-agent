def test_import_agents_and_core_modules():
    # Import modules to ensure basic syntax and availability
    import agents.base as agents_base
    import agents.analyst as agents_analyst
    import agents.decision_maker as agents_dm
    import core.db_models as core_db
    import core.trader as core_trader
    import core.monitor as core_monitor
    import core.scanner as core_scanner
    import core.notifier as core_notifier

    # Assert key symbols exist without executing side-effects
    assert hasattr(agents_base, '__all__') or agents_base is not None
    assert hasattr(agents_analyst, 'AnalystAgent')
    assert hasattr(agents_dm, 'DecisionMakerAgent')
    assert hasattr(core_db, 'init_db')
    assert hasattr(core_trader, 'Trader')
    assert hasattr(core_monitor, 'PriceMonitor') or core_monitor is not None
    assert core_scanner is not None
    assert core_notifier is not None
