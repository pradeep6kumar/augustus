#!/usr/bin/env python

# Copyright (C) 2006-2011  Open Data ("Open Data" refers to
# one or more of the following companies: Open Data Partners LLC,
# Open Data Research LLC, or Open Data Capital LLC.)
#
# This file is part of Augustus.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Defines the k-means clustering producer and consumer algorithms."""

# system includes
import math
import numpy
import new

# local includes
from augustus.core.defs import INVALID, MISSING
from augustus.algorithms.defs import ConsumerAlgorithm, ProducerAlgorithm
from augustus.algorithms.eventweighting import COUNT, SUM1, SUMX, SUMXX, COVARIANCE

import augustus.core.pmml41 as pmml

SCORE_predictedValue = pmml.OutputField.predictedValue
SCORE_clusterId = pmml.OutputField.clusterId
SCORE_clusterAffinity = pmml.OutputField.clusterAffinity
SCORE_entityId = pmml.OutputField.entityId

#########################################################################################
######################################################################### consumer ######
#########################################################################################

class ConsumerClusteringModel(ConsumerAlgorithm):
    def initialize(self, **params):
        """Initialize a clustering model consumer."""

        self.model = self.segmentRecord.pmmlModel

    def score(self, syncNumber, get):
        """Score one event with the clustering model, returning a scores dictionary."""

        vector = [get(field) for field in self.model.fields]
        if INVALID in vector: return INVALID
        
        result = self.model.closestCluster(vector)
        if result is INVALID: return INVALID

        clusterId, clusterNumber, clusterAffinity = result
        return {SCORE_predictedValue: clusterId,
                SCORE_clusterId: clusterNumber, SCORE_clusterAffinity: clusterAffinity,
                SCORE_entityId: clusterNumber}

#########################################################################################
######################################################################### producer ######
#########################################################################################

class TrialClusterSet:
    """A set of cluster centroids that aspires to be the solution to
    the clustering problem.

    Only called by ProducerClusteringModel.
    """

    def __init__(self, numberOfClusters, trans, shift, updateScheme):
        self.updator = updateScheme.updator(COUNT, SUM1, SUMX, SUMXX)
        self.clusters = [TrialCluster(trans, shift, updateScheme) for i in xrange(numberOfClusters)]

    def __repr__(self):
        return "<TrialClusterSet %d %g>" % (self.updator.count(), self.updator.mean())

    def __str__(self):
        return " ".join(map(repr, self.clusters))

    def update(self, syncNumber, vector, model, moving):
        anyMissing = MISSING in vector

        # find the cluster to which this vector of data is closest
        shortestDistance = None
        closestCluster = None
        for cluster in self.clusters:
            if moving:
                clusterVector = cluster.vector()
            else:
                clusterVector = cluster.initialPosition

            if anyMissing:
                distance = model.metricMissing.metric(vector, clusterVector)
            else:
                distance = model.metric.metric(vector, clusterVector)

            if shortestDistance is None or distance < shortestDistance:
                shortestDistance = distance
                closestCluster = cluster

        # XSD requires at least one cluster, so shortestDistance and closestCluster cannot be None

        # the Lamarckian step: clusters approach the center of the data points in its realm
        # (giraffes stretch their necks to reach the high leaves)
        closestCluster.increment(syncNumber, vector)

        # quality of this cluster set as a whole
        self.updator.increment(syncNumber, shortestDistance)

class TrialCluster:
    """A single cluster centroid.

    Only called by ProducerClusteringModel.
    """

    def __init__(self, trans, shift, updateScheme):
        # generate a random point (from a distribution with a covariance like the data)
        randomPoint = numpy.matrix(numpy.random.randn(len(shift))).T
        randomPoint = (trans * randomPoint + shift)

        # set up a vector of updators with their initial value at the random point
        self.fields = []
        self.initialPosition = []
        for i in xrange(len(shift)):
            u = updateScheme.updator(SUM1, SUMX)
            u.initialize({SUM1: 1., SUMX: randomPoint[i,0]})
            self.fields.append(u)
            self.initialPosition.append(randomPoint[i,0])

    def __repr__(self):
        return "<TrialCluster %s>" % [u.mean() for u in self.fields]

    def vector(self):
        return [u.mean() for u in self.fields]

    def increment(self, syncNumber, vector):
        for i, u in enumerate(self.fields):
            u.increment(syncNumber, vector[i])

class ProducerClusteringModel(ProducerAlgorithm):
    """An event-based clustering algorithm.

    Although it does not iterate over the data as k-means does, it
    converges quickly because it creates large ensembles of trial
    clustering solutions.
    """

    def initialize(self, **params):
        """Initialize a clustering model producer."""

        if "resume" in params:
            self.resume = pmml.boolCheck(params["resume"])
            del params["resume"]
        else:
            self.resume = False

        if "numberOfTrials" in params:
            self.numberOfTrials = int(params["numberOfTrials"])
            del params["numberOfTrials"]
        else:
            self.numberOfTrials = 10

        if "numberToKeep" in params:
            self.numberToKeep = int(params["numberToKeep"])
            del params["numberToKeep"]
        else:
            self.numberToKeep = 3

        if "maturityThreshold" in params:
            self.maturityThreshold = int(params["maturityThreshold"])
            del params["maturityThreshold"]
        else:
            self.maturityThreshold = 100

        if "initialStability" in params:
            self.initialStability = int(params["initialStability"])
            del params["initialStability"]
        else:
            self.initialStability = 100

        if "overrideSignificance" in params:
            self.overrideSignificance = float(params["overrideSignificance"])
            del params["overrideSignificance"]
            if self.overrideSignificance == 0.:
                self.overrideSignificance = None
        else:
            self.overrideSignificance = 5.

        self.model = self.segmentRecord.pmmlModel
        self.dataDistribution = self.engine.producerUpdateScheme.updator(COVARIANCE(self.model.numberOfFields))

        self.distanceMeasure = (self.model.child(pmml.ComparisonMeasure).attrib["kind"] == "distance")
            
        # put PartialSums in the model if they're not already there; pick up old values if you're resuming
        extension = self.model.child(pmml.Extension, exception=False)
        if extension is None:
            extension = pmml.Extension()
            self.model.children.append(extension)

        if self.resume:
            self.sumOfDistances = extension.child(lambda x: isinstance(x, pmml.X_ODG_PartialSums) and x.attrib.get("name", None) == "SumOfDistances", exception=False)
        else:
            index = extension.index(lambda x: isinstance(x, pmml.X_ODG_PartialSums) and x.attrib.get("name", None) == "SumOfDistances", exception=False)
            if index is not None:
                del extension[index]
            self.sumOfDistances = None

        if self.sumOfDistances is None:
            self.sumOfDistances = pmml.X_ODG_PartialSums(name="SumOfDistances", COUNT=0, SUM1=0., SUMX=0., SUMXX=0.)
            extension.children.append(self.sumOfDistances)

        self.partialSums = {}
        for theid, cluster in zip(self.model.ids, self.model.cluster):
            for i, field in enumerate(self.model.fields):
                fullname = "%s.%s" % (theid, field)

                if self.resume:
                    partialSum = extension.child(lambda x: isinstance(x, pmml.X_ODG_PartialSums) and x.attrib.get("name", None) == fullname, exception=False)
                else:
                    index = extension.index(lambda x: isinstance(x, pmml.X_ODG_PartialSums) and x.attrib.get("name", None) == fullname, exception=False)
                    if index is not None:
                        del extension[index]
                    partialSum = None

                if partialSum is None:
                    partialSum = pmml.X_ODG_PartialSums(name=fullname, SUM1=1., SUMX=cluster.value[i])
                    extension.children.append(partialSum)

                self.partialSums[fullname] = partialSum
                    
        # create the first trial using the values constructed above (they come from the PMML file if resume is True)
        trialFromPmml = new.instance(TrialClusterSet)
        trialFromPmml.updator = self.engine.producerUpdateScheme.updator(COUNT, SUM1, SUMX, SUMXX)
        trialFromPmml.updator.initialize({COUNT: self.sumOfDistances.attrib["COUNT"], SUM1: self.sumOfDistances.attrib["SUM1"], SUMX: self.sumOfDistances.attrib["SUMX"], SUMXX: self.sumOfDistances.attrib["SUMXX"]})

        trialFromPmml.clusters = []
        for theid, cluster in zip(self.model.ids, self.model.cluster):
            trialCluster = new.instance(TrialCluster)
            trialCluster.fields = []
            trialCluster.initialPosition = []
            for field in self.model.fields:
                partialSum = self.partialSums["%s.%s" % (theid, field)]
                u = self.engine.producerUpdateScheme.updator(SUM1, SUMX)
                u.initialize({SUM1: partialSum.attrib["SUM1"], SUMX: partialSum.attrib["SUMX"]})
                trialCluster.fields.append(u)
            trialCluster.initialPosition = list(cluster.value)
            trialFromPmml.clusters.append(trialCluster)

        self.trials = [trialFromPmml]

        if len(params) > 0:
            raise TypeError, "Unrecognized parameters %s" % params

    def update(self, syncNumber, get):
        """Update the clustering model.

        This will move all clusters
        closer to their nearest data points (Lamarckian step), but it
        may not change the current champion (Darwinian step).
        """

        vector = [get(field) for field in self.model.fields]
        if INVALID in vector or MISSING in vector: return False

        if self.distanceMeasure:
            self.dataDistribution.increment(syncNumber, vector)

            try:
                covariance = self.dataDistribution.covariance()
                shift = self.dataDistribution.covmean()
            except ZeroDivisionError:
                return False
            if covariance is INVALID:
                return False

            # characterize the data so that you can generate random numbers with the same distribution
            try:
                trans = numpy.linalg.cholesky(covariance)
            except numpy.linalg.LinAlgError:
                return False

        else:
            trans = numpy.matrix(numpy.identity(len(self.model.fields)))
            shift = numpy.matrix(numpy.zeros(len(self.model.fields))).T
            raise NotImplementedError, "Currently, only clusters with ComparisonMeasure.kind == 'distance' metrics can be produced."

        while len(self.trials) < self.numberOfTrials:
            self.trials.append(TrialClusterSet(self.model.numberOfClusters, trans, shift, self.engine.producerUpdateScheme))

        for trial in self.trials:
            trial.update(syncNumber, vector, self.model, trial.updator.count() > self.initialStability)

        if self.overrideSignificance is None:
            self.trials.sort(lambda a, b: cmp(a.updator.mean(), b.updator.mean()))
        else:
            sig = self.overrideSignificance
            def sorter(a, b):
                amean = a.updator.mean()
                bmean = b.updator.mean()
                avar = a.updator.variance()
                bvar = b.updator.variance()
                if avar < 0.: avar = 0.
                if bvar < 0.: bvar = 0.
                return cmp(amean + sig*math.sqrt(avar)/a.updator.counters[SUM1], bmean + sig*math.sqrt(bvar)/b.updator.counters[SUM1])
            self.trials.sort(sorter)

        # the Darwinian step: keep only the N fittest (after they become mature)
        immature = []
        keep = []
        for trial in self.trials:
            if trial.updator.count() < self.maturityThreshold:
                immature.append(trial)
            elif len(keep) < self.numberToKeep:
                keep.append(trial)

        self.trials = immature + keep

        # write the current winner to the PMML file
        if len(keep) > 0:
            best = keep[0]
            self.sumOfDistances.attrib["COUNT"] = best.updator.counters[COUNT]
            self.sumOfDistances.attrib["SUM1"] = best.updator.counters[SUM1]
            self.sumOfDistances.attrib["SUMX"] = best.updator.counters[SUMX]
            self.sumOfDistances.attrib["SUMXX"] = best.updator.counters[SUMXX]

            for trialCluster, pmmlCluster, theid in zip(best.clusters, self.model.cluster, self.model.ids):
                pmmlCluster.value = trialCluster.vector()

                for field, u in zip(self.model.fields, trialCluster.fields):
                    partialSum = self.partialSums["%s.%s" % (theid, field)]
                    partialSum.attrib["SUM1"] = u.counters[SUM1]
                    partialSum.attrib["SUMX"] = u.counters[SUMX]

            return True

        else:
            return False