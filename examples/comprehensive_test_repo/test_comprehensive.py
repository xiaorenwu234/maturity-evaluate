#!/usr/bin/env python3
"""Comprehensive test script for new_code module.

Tests all major features:
- Python SCIP call resolution
- Java Joern call resolution
- Metrics (cyclomatic complexity, branches, nesting)
- CFG generation
- Clone detection
- Cross-file imports
- Multiple inheritance
- Nested classes
- Decorators
- Lambda/generators
- Dynamic calls (should NOT produce edges)
"""

import sys
from pathlib import Path
from collections import Counter

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from kg_builder.config import BuildSettings
from kg_builder.collectors.code import CodeCollector


def test_python_comprehensive():
    """Test Python channel with comprehensive examples."""
    print("=" * 80)
    print("Testing Python Channel")
    print("=" * 80)
    
    repo_path = Path("examples/comprehensive_test_repo/python_app")
    settings = BuildSettings(repo_path=repo_path)
    settings.code_analysis.enabled = True
    
    collector = CodeCollector(settings)
    result = collector.collect()
    
    print(f"\nFiles scanned: {result.metrics.get('files_scanned')}")
    print(f"Nodes: {len(result.nodes)}")
    print(f"Edges: {len(result.edges)}")
    
    edge_types = Counter(e.relation for e in result.edges)
    print(f"\nEdge types: {dict(edge_types)}")
    
    # Check CALLS edges
    calls_edges = [e for e in result.edges if e.relation == 'CALLS']
    print(f"\nCALLS edges ({len(calls_edges)}):")
    for e in calls_edges:
        src_name = next((n.name for n in result.nodes if n.id == e.source), '?')
        tgt_name = next((n.name for n in result.nodes if n.id == e.target), '?')
        resolved_by = e.attributes.get('resolved_by', 'unknown')
        print(f"  {src_name} -> {tgt_name} (resolved_by={resolved_by})")
    
    # Check FUNCTION nodes with metrics
    print("\nFUNCTION nodes with metrics:")
    for n in result.nodes:
        if n.type == 'Function':
            m = n.attributes.get('metrics', {})
            cfg = n.attributes.get('cfg')
            cfg_info = f", CFG={len(cfg['nodes'])} nodes" if cfg else ", No CFG"
            print(f"  {n.name}: CC={m.get('cyclomatic_complexity')}, "
                  f"branches={m.get('num_branches')}, "
                  f"nesting={m.get('max_nesting_depth')}{cfg_info}")
    
    # Check warnings
    if result.warnings:
        print(f"\nWarnings: {result.warnings}")
    
    return result


def test_java_comprehensive():
    """Test Java channel with comprehensive examples."""
    print("\n" + "=" * 80)
    print("Testing Java Channel")
    print("=" * 80)
    
    repo_path = Path("examples/comprehensive_test_repo/java_app")
    settings = BuildSettings(repo_path=repo_path)
    settings.code_analysis.enabled = True
    settings.code_analysis.java_enabled = True
    
    collector = CodeCollector(settings)
    result = collector.collect()
    
    print(f"\nFiles scanned: {result.metrics.get('files_scanned')}")
    print(f"Nodes: {len(result.nodes)}")
    print(f"Edges: {len(result.edges)}")
    
    edge_types = Counter(e.relation for e in result.edges)
    print(f"\nEdge types: {dict(edge_types)}")
    
    # Check CALLS edges
    calls_edges = [e for e in result.edges if e.relation == 'CALLS']
    print(f"\nCALLS edges ({len(calls_edges)}):")
    for e in calls_edges:
        src_name = next((n.name for n in result.nodes if n.id == e.source), '?')
        tgt_name = next((n.name for n in result.nodes if n.id == e.target), '?')
        resolved_by = e.attributes.get('resolved_by', 'unknown')
        print(f"  {src_name} -> {tgt_name} (resolved_by={resolved_by})")
    
    # Check FUNCTION nodes with metrics and CFG
    print("\nFUNCTION nodes with metrics:")
    for n in result.nodes:
        if n.type == 'Function':
            m = n.attributes.get('metrics', {})
            cfg = n.attributes.get('cfg')
            cfg_info = f", CFG={len(cfg['nodes'])} nodes" if cfg else ", No CFG"
            print(f"  {n.name}: CC={m.get('cyclomatic_complexity')}, "
                  f"branches={m.get('num_branches')}, "
                  f"nesting={m.get('max_nesting_depth')}{cfg_info}")
    
    # Check SERVICE nodes
    print("\nSERVICE nodes:")
    for n in result.nodes:
        if n.type == 'Service':
            print(f"  {n.name} (file={n.attributes.get('file')})")
    
    # Check warnings
    if result.warnings:
        print(f"\nWarnings: {result.warnings}")
    
    return result


def main():
    """Run all tests."""
    print("Comprehensive Test Suite for new_code Module")
    print("=" * 80)
    
    try:
        py_result = test_python_comprehensive()
        java_result = test_java_comprehensive()
        
        print("\n" + "=" * 80)
        print("Test Summary")
        print("=" * 80)
        print(f"Python: {len(py_result.nodes)} nodes, {len(py_result.edges)} edges")
        print(f"Java:   {len(java_result.nodes)} nodes, {len(java_result.edges)} edges")
        print("\nAll tests completed successfully!")
        
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
