# Advanced prototype:
# Does GC at user-specified intervals
# Random number of edges due to modeling
# recombination as a Poisson process.

import numpy as np
import msprime
import sys
import argparse

node_dt = np.dtype([('id', np.uint32), ('generation', np.float), ('population', np.int32)])
edge_dt = np.dtype([('left', np.float), ('right', np.float), ('parent', np.int32), ('child', np.int32)])

# Simulation with be popsize*SIMLEN generations
SIMLEN = 20

class MockAncestryTracker(object):
    """
    Mimicking the public API of AncestryTracker.
    """
    nodes = None
    edges = None
    samples = None
    anc_samples = None

    def __init__(self):
        self.nodes = np.empty([0], dtype=node_dt)
        self.edges = np.empty([0], dtype=edge_dt)
        self.samples = np.empty([0], dtype=np.uint32)
        self.anc_samples = np.empty([0], dtype=np.uint32)

    def update_data(self, new_nodes, new_edges, new_samples, new_anc_samples):
        """
        Takes the new nodes and edges simulated each generation
        and appends them to the class data.  new_samples is the list
        of node IDs corresponding to the current children.
        """
        self.nodes = np.insert(self.nodes, len(self.nodes), new_nodes)
        self.edges = np.insert(self.edges, len(self.edges), new_edges)
        self.samples = new_samples
        self.anc_samples = np.insert(self.anc_samples, len(self.anc_samples), new_anc_samples)

    def convert_time(self):
        """
        Convert from forwards time to backwards time.

        This results in self.nodes['generation'].min() 
        equal to -0.0 (e.g, 0.0 with the negative bit set),
        but msprime does not seem to mind.
        """
        self.nodes['generation'] -= self.nodes['generation'].max()
        self.nodes['generation'] *= -1.0

    def reset_data(self):
        """
        Call this after garbage collection.
        Any necessary post-processing is done here.
        """
        self.nodes = np.empty([0], dtype=node_dt)
        self.edges = np.empty([0], dtype=edge_dt)

    def post_gc_cleanup(self, gc_rv):
        """
        Pass in the return value from ARGsimplifier.__call__.

        We clean up internal data if needed.
        """
        if gc_rv[0] is True:
            self.reset_data()

class ARGsimplifier(object):
    """
    Mimicking the API if our Python
    class to collect simulated
    results and process them via msprime
    """
    nodes = None
    edges = None
    sites = None
    gc_interval = None
    last_gc_time = None

    def __init__(self, gc_interval=None):
        self.nodes = msprime.NodeTable()
        self.edges = msprime.EdgeTable()
        self.gc_interval = gc_interval
        self.last_gc_time = 0

    def simplify(self, generation, tracker):
        """
        Details of taking new data, appending, and
        simplifying.

        :return: length of simplifed node table, which is next_id to use
        """
        # Update time in current nodes.
        # Is this most efficient method?
        node_offset = 0

        if(generation == 1):
            prior_ts = msprime.simulate(sample_size=2 * args.popsize, Ne=2 * args.popsize, random_seed=args.seed)
            prior_ts.dump_tables(nodes=self.nodes, edges=self.edges)
            self.nodes.set_columns(flags=self.nodes.flags, population=self.nodes.population, time=self.nodes.time + generation)
            # already indexed to be after the first wave of generation (at population size 2 * args.popsize)
            # so just need to offset by the number of additional coalescent nodes
            node_offset = self.nodes.num_rows - 2 * args.popsize
        else:
            dt = generation - self.last_gc_time
            self.nodes.set_columns(flags=self.nodes.flags, population=self.nodes.population, time=self.nodes.time + dt)

        # Create "flags" for new nodes.
        # This is much faster than making a list
        flags = np.empty([len(tracker.nodes)], dtype=np.uint32)
        flags.fill(1)

        # Convert time from forwards to backwards
        tracker.convert_time()

       # Update internal *Tables
        self.nodes.append_columns(flags=flags, population=tracker.nodes['population'], time=tracker.nodes['generation'])
        self.edges.append_columns(left=tracker.edges['left'], right=tracker.edges['right'], parent=tracker.edges['parent'], child=tracker.edges['child'] + node_offset)
                                  # only offset child node ids (and sample ids), for a single simulation generation, edge parents will be correct

        # Sort and simplify
        msprime.sort_tables(nodes=self.nodes, edges=self.edges)
        # set guards against duplicate node ids when an ancestral sample generation overlaps with a gc generation
        # sorting the set in reverse order ensures that generational samples occur *before* ancestral samples in the node table,
        # making bookkeeping easier during the WF (parents of the next generation are guaranteed to be 0-2N in the node table)
        all_samples = sorted(set((tracker.anc_samples + node_offset).tolist() + (tracker.samples + node_offset).tolist()), reverse=True)
        node_map = msprime.simplify_tables(samples=all_samples, nodes=self.nodes, edges=self.edges)

        tracker.anc_samples = np.array([node_map[int(node_id)] for node_id in tracker.anc_samples])
        # Return length of NodeTable,
        # which can be used as next offspring ID
        return self.nodes.num_rows

    def __call__(self, generation, tracker):
        if generation > 0 and (generation % self.gc_interval == 0 or generation == 1):
            next_id = self.simplify(generation, tracker)
            self.last_gc_time = generation
            return (True, next_id)

        return (False, None)

def wf(N, simplifier, tracker, anc_sample_gen, ngens):
    """
    For N diploids, the diploids list contains 2N values.
    For the i-th diploids, diploids[2*i] and diploids[2*i+1]
    represent the two chromosomes.

    We simulate constant N here, according to a Wright-Fisher
    scheme:

    1. Pick two parental indexes, each [0,N-1]
    2. The two chromosome ids are diploids[2*p] and diploids[2*p+1]
       for each parental index 'p'.
    3. We pass one parental index on to each offspring, according to
       Mendel, which means we swap parental chromosome ids 50% of the time.
    4. Crossing over is a Poisson process, and the code used here (above)
       is modeled after that in our C++ implementation.
    5. Mutation is a Poisson process, mutations are added according to each
       parental index. 
    """
    diploids = np.arange(2 * N, dtype=np.uint32)
    # so we pre-allocate the space. We only need
    # to do this once b/c N is constant.
    tracker.nodes = np.empty([0], dtype=node_dt)

    ancestral_gen_counter = 0
    next_id = len(diploids)  # This will be the next unique ID to use
    for gen in range(ngens):
        ancestral_samples = []
        # Let's see if we will do some GC:
        gc_rv = simplifier(gen, tracker)
        # If so, let the tracker clean up:
        tracker.post_gc_cleanup(gc_rv)
        if gc_rv[0] is True:
            # If we did GC, we need to reset
            # some variables.  Internally,
            # when msprime simplifies tables,
            # the 2N tips are entries 0 to 2*N-1
            # in the NodeTable, hence the
            # re-assignment if diploids.
            # We can also reset next_id to the
            # length of the current NodeTable,
            # keeping risk of integer overflow
            # to a minimum.
            next_id = int(gc_rv[1])
            diploids = np.arange(2 * N, dtype=np.uint32)

        # Empty offspring list.
        # We also use this to mark the "samples" for simplify
        new_diploids = np.empty([len(diploids)], dtype=diploids.dtype)

        if(ancestral_gen_counter < len(anc_sample_gen) and gen + 1 == anc_sample_gen[ancestral_gen_counter][0]):
            ran_samples = np.random.choice(int(N), int(anc_sample_gen[ancestral_gen_counter][1]), replace=False)
            # while sorting to get diploid chromosomes next to each other isn't strictly necessary,
            # they will be sorted (in reverse order) before simplication anyway, no need to do it here
            ancestral_samples = np.concatenate((2*ran_samples + next_id, 2*ran_samples + 1 + next_id))
            ancestral_gen_counter += 1

        # Store temp nodes for this generation.
        # b/c we add 2N nodes each geneartion,
        # We can allocate it all at once.
        nodes = np.empty([2 * N], dtype=node_dt)
        nodes['population'].fill(0)
        nodes['generation'] = gen + 1
        nodes['id'] = np.arange(start=next_id, stop=next_id + 2 * N, dtype=nodes['id'].dtype)

        # Store temp edges for this generation
        edges = []  # np.empty([0], dtype=edge_dt)

        # Pick 2N parents:
        parents = np.random.randint(0, N, 2 * N)
        dip = int(0)  # dummy index for filling contents of new_diploids

        # Iterate over our chosen parents via fancy indexing.
        for parent1, parent2 in zip(parents[::2], parents[1::2]):
            # p1g1 = parent 1, gamete (chrom) 1, etc.:
            p1g1, p1g2 = diploids[2 * parent1], diploids[2 * parent1 + 1]
            p2g1, p2g2 = diploids[2 * parent2], diploids[2 * parent2 + 1]

            # Apply Mendel to p1g1 et al.
            mendel = np.random.random_sample(2)
            if mendel[0] < 0.5:
                p1g1, p1g2 = p1g2, p1g1
            if mendel[1] < 0.5:
                p2g1, p2g2 = p2g2, p2g1

            edges.append((0.0, 1.0, p1g1, next_id))
            # Update offspring container for
            # offspring dip, chrom 1:
            new_diploids[2 * dip] = next_id

            # Repeat process for parent 2's contribution.
            # Stuff is now being inherited by node next_id + 1
            edges.append((0.0, 1.0, p2g1, next_id+1))

            new_diploids[2 * dip + 1] = next_id + 1

            # Update our dummy variables.
            next_id += 2
            dip += 1

        diploids = new_diploids
        tracker.update_data(nodes, edges, diploids, ancestral_samples)

    return (diploids)
    
def parse_args():
    dstring = "Prototype implementation of ARG tracking and regular garbage collection."
    parser = argparse.ArgumentParser(description=dstring, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--popsize', '-N', type=int, default=500, help="Diploid population size")
    parser.add_argument('--nsam', '-n', type=int, default=5, help="Sample size (in diploids).")
    parser.add_argument('--seed', '-S', type=int, default=42, help="RNG seed")
    parser.add_argument('--gc', '-G', type=int, default=100, help="GC interval")
    
    return parser

if __name__ == "__main__":
    parser = parse_args()
    args = parser.parse_args(sys.argv[1:])
    np.random.seed(args.seed)

    simplifier = ARGsimplifier(args.gc)
    tracker = MockAncestryTracker()
    ngens = SIMLEN * args.popsize
    anc_sample_gen = [(ngens * (i + 1) / SIMLEN, max(round(args.popsize / 200), 1)) for i in range(SIMLEN - 2)]
    samples = wf(args.popsize, simplifier, tracker, anc_sample_gen, ngens)

    if len(tracker.nodes) > 0:  # Then there's stuff that didn't get GC'd
        simplifier.simplify(ngens, tracker)

    ran_samples = np.random.choice(args.popsize, args.nsam, replace=False)
    nsam_samples = sorted(np.concatenate((2*ran_samples, 2*ran_samples + 1)))
    all_samples = nsam_samples + tracker.anc_samples.tolist()
    node_map = msprime.simplify_tables(samples=all_samples, nodes=simplifier.nodes, edges=simplifier.edges)