from graph_engine.boot.graph_revision import FeatureFactoryRef

FEATURE_GRAPH_FACTORIES = (
    FeatureFactoryRef("assurance.intake", "assurance_intake.graphs.factory:build_intake_graphs"),
    FeatureFactoryRef(
        "assurance.generation",
        "assurance_generation.graphs.factory:build_generation_graphs",
    ),
    FeatureFactoryRef(
        "assurance.execution",
        "assurance_execution.graphs.factory:build_execution_graphs",
    ),
    FeatureFactoryRef("assurance.quality", "assurance_quality.graphs.factory:build_quality_graphs"),
    FeatureFactoryRef("assurance.healing", "assurance_healing.graphs.factory:build_healing_graphs"),
    FeatureFactoryRef(
        "assurance.improvement",
        "assurance_improvement.graphs.factory:build_improvement_graphs",
    ),
)
